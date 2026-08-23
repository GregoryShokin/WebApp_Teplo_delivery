"""Телеграм-бот приёмки документов: переслал фотографию — строка появилась в очереди оплат.

ЗАЧЕМ ИМЕННО БОТ. Арендодатели присылают квитанции в мессенджер (владелец, 02.08.2026), и
единственное действие, которое от человека вообще требуется, — переслать сообщение. Кнопка
«Загрузить документ» на «Странице на оплату» требует сохранить фото, открыть браузер, найти
файл; на трёх документах в месяц это ровно та цена, из-за которой учёт и не ведут.

БОТ НИЧЕГО НЕ РЕШАЕТ. Он доставляет файл в ту же дверь, что и кнопка, — ``utility_intake``.
Распознавание, выбор потока, создание документов и все отказы живут там: иметь вторую копию
правил «что считать долгом» значит однажды получить два разных ответа на один документ.

ПОЛЛИНГ, А НЕ ВЕБХУК. Вебхук требует публичного адреса и секрета на нём; long polling —
только исходящего соединения, поэтому работает и на стенде, и с ноутбука. Курсор при этом
хранит САМ Телеграм: подтверждённые ``offset``-ом обновления он больше не отдаёт, поэтому своей
таблицы под курсор не нужно, а после перезапуска приложение получит ровно неподтверждённое.
Повтор того же файла безвреден и без курсора — приёмка узнаёт его по SHA-256.

БЕЗ БЕЛОГО СПИСКА (решение владельца 02.08.2026). Документ принимается от любого, кто написал
боту: список чатов пришлось бы вести руками при каждом новом отправителе, а имя бота нигде не
объявлено. Взамен приёмка запоминает ОТПРАВИТЕЛЯ — имя и chat id уходят в поле «от кого» той же
строки, что видна на «Странице на оплату». Запрета нет, но вопрос «чей это документ и с кого
спрашивать» отвечается всегда, а сомнительную строку человек исключает одной кнопкой.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import EmailInvoiceIntake
from app.services import utility_intake

logger = logging.getLogger(__name__)

__all__ = ["TelegramIntakeError", "is_stale", "poll_and_ingest", "sender_label"]

API_BASE = "https://api.telegram.org"

# Телеграм отдаёт файл только через getFile и не больше 20 МБ — свой предел приёмки (25 МБ)
# сюда не достаёт, и упереться в него можно лишь документом, который бот и не получит.
_HTTP_TIMEOUT = 60.0

# Курсор подтверждения. В памяти процесса намеренно: источник правды — сам Телеграм, он
# удаляет обновления, подтверждённые следующим ``offset``. Перезапуск приложения приводит к
# повторной выдаче НЕподтверждённого, а это ровно то, что нужно.
_next_offset: int | None = None


class TelegramIntakeError(RuntimeError):
    """Разговор с Телеграмом не состоялся. Проход прекращается, курсор не двигается."""


def is_stale(message: dict[str, Any], *, max_age_seconds: int, now_ts: float) -> bool:
    """Сообщение старше отсечки? Такие не берём в работу, но подтверждаем.

    Телеграм держит неподтверждённые обновления сутки и отдаёт их все разом при первом
    обращении. Для приложения, которое только что задеплоили, это значит пачку вчерашних
    документов — часть из них к тому времени уже проведена руками, и повтор пришлось бы
    разбирать. Плюс ссылка на файл в Телеграме живёт около часа: у старого сообщения снимок
    всё равно уже не скачается, и «обработка» кончилась бы ошибкой.

    Сообщение без даты не отбрасываем: сомнение толкуем в пользу документа.
    """
    if max_age_seconds <= 0:
        return False
    sent_at = message.get("date")
    if not isinstance(sent_at, int | float):
        return False
    return (now_ts - float(sent_at)) > max_age_seconds


def sender_label(message: dict[str, Any]) -> str:
    """«Кто прислал» одной строкой — она ложится в поле «от кого» и видна в списке.

    Белого списка нет, поэтому отправитель — единственное, что отвечает на вопрос «чей это
    документ». Имя берём как в мессенджере, chat id оставляем рядом: имена меняются и
    повторяются, id — нет, и по нему человека находят однозначно.
    """
    sender = message.get("from") or {}
    chat_id = (message.get("chat") or {}).get("id")
    name = " ".join(
        part
        for part in (sender.get("first_name"), sender.get("last_name"))
        if isinstance(part, str) and part
    )
    username = sender.get("username")
    if username:
        name = f"{name} @{username}".strip() if name else f"@{username}"
    return f"Telegram · {name or 'без имени'} ({chat_id})"


# Токен бота стоит в ПУТИ запроса (`/bot<токен>/getUpdates`), а httpx на уровне INFO пишет URL
# целиком. В проде логи идут именно на INFO, и опрос тикает каждые полминуты — без этого фильтра
# секрет попадал бы в журнал сотни раз в сутки, откуда его читает любой, у кого есть доступ к
# логам контейнера. Один раз этого уже хватило, чтобы токен пришлось отзывать.
_TOKEN_IN_URL = re.compile(r"(/bot)(\d+):[A-Za-z0-9_-]+")


class _RedactBotToken(logging.Filter):
    """Заменить токен в тексте записи на звёздочки. Номер бота оставляем — он не секрет."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 — сломанный формат записи не наше дело
            return True
        cleaned = _TOKEN_IN_URL.sub(r"\1\2:***", text)
        if cleaned != text:
            # Подстановка уже выполнена, поэтому args обнуляем: иначе logging попытается
            # форматировать готовую строку второй раз и упадёт на первом же проценте.
            record.msg = cleaned
            record.args = ()
        return True


for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).addFilter(_RedactBotToken())


def _make_client() -> httpx.AsyncClient:
    """Точка, которую подменяет тест. Своей роли, кроме этой, у обёртки нет."""
    return httpx.AsyncClient(timeout=_HTTP_TIMEOUT)


async def _call(client: httpx.AsyncClient, token: str, method: str, **params: Any) -> Any:
    response = await client.post(f"{API_BASE}/bot{token}/{method}", json=params)
    if response.status_code >= 400:
        raise TelegramIntakeError(f"{method}: HTTP {response.status_code}")
    payload = response.json()
    if not payload.get("ok"):
        raise TelegramIntakeError(f"{method}: {payload.get('description') or 'отказ Телеграма'}")
    return payload.get("result")


async def _notify(client: httpx.AsyncClient, token: str, chat_id: Any, text: str) -> None:
    """Ответить в чат. Не бросает никогда.

    Ответ важен, но он вторичен: документ к этому моменту уже заведён, и падать из-за того, что
    собеседник заблокировал бота, — значит откатывать сделанную работу и требовать её заново.
    """
    try:
        await _call(client, token, "sendMessage", chat_id=chat_id, text=text)
    except Exception:  # noqa: BLE001 — недоставленный ответ не отменяет заведённый документ
        logger.warning("telegram_intake: ответ в чат %s не доставлен", chat_id, exc_info=True)


async def _download(client: httpx.AsyncClient, token: str, file_path: str) -> bytes:
    response = await client.get(f"{API_BASE}/file/bot{token}/{file_path}")
    if response.status_code >= 400:
        raise TelegramIntakeError(f"скачивание файла: HTTP {response.status_code}")
    return response.content


def _pick_file(message: dict[str, Any]) -> tuple[str, str | None] | None:
    """Что в сообщении можно принять как документ.

    Документ предпочтительнее фотографии: Телеграм сжимает фото, а мелкие цифры в таблице
    квитанции — единственное, ради чего снимок и нужен. Если пришло и то и другое, документ
    выигрывает. Из размеров фотографии берём самый крупный по той же причине.
    """
    document = message.get("document")
    if isinstance(document, dict) and document.get("file_id"):
        return str(document["file_id"]), document.get("file_name")
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        largest = max(photos, key=lambda size: int(size.get("file_size") or 0))
        return str(largest["file_id"]), None
    return None


def _row_line(row: EmailInvoiceIntake) -> str:
    """Одна строка ответа про один заведённый документ."""
    recognition = row.recognition or {}
    utility = recognition.get("utility") or {}
    title = recognition.get("invoice_number") or "Документ"
    payable = utility.get("payable_amount") or recognition.get("amount")
    expense = utility.get("expense_amount")

    if row.status == "linked":
        line = f"✅ {title}: к оплате {payable} ₽"
        # Расход называем, только когда он расходится с платежом: у воды это одно число, и
        # повторять его дважды значит делать вид, что чисел два.
        if expense and expense != payable:
            line += f", расход за период {expense} ₽"
        hints = utility.get("hints") or []
        if hints:
            line += "\n   " + "; ".join(hints)
        return line
    if row.status == "duplicate":
        return f"🔁 {title}: за этот месяц документ уже заведён — второй долг не создан"
    blocking = utility.get("blocking") or []
    reason = "; ".join(blocking) if blocking else (row.error or "разобрать не удалось")
    return f"⚠️ Нужны руки: {reason}.\n   Откройте «Финансы → Платежи» → «Разобрать»"


def _reply_text(rows: list[EmailInvoiceIntake]) -> str:
    if not rows:
        return "⚠️ Обработку закончил, но ничего не разобрал — откройте «Финансы → Платежи»"
    if all(row.status == "linked" for row in rows):
        head = (
            "✅ Готово — документ обработан"
            if len(rows) == 1
            else f"✅ Готово — обработано документов в файле: {len(rows)}"
        )
    elif any(row.status not in ("linked", "duplicate") for row in rows):
        head = "⚠️ Обработку закончил, но документ требует проверки"
    else:
        head = "Обработку закончил"
    return head + "\n" + "\n".join(_row_line(row) for row in rows)


async def _handle_message(
    session: AsyncSession,
    client: httpx.AsyncClient,
    *,
    token: str,
    settings: Settings,
    message: dict[str, Any],
) -> str:
    """Обработать одно сообщение. Возвращает исход для счётчиков прохода."""
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id is None:
        return "skipped"

    picked = _pick_file(message)
    if picked is None:
        await _notify(
            client,
            token,
            chat_id,
            "Пришлите фотографию или файл квитанции — счёт за воду, газ или акт "
            "за электричество. Пересланное сообщение тоже подойдёт.",
        )
        return "no_file"

    # Ответ отправляем ДО getFile, скачивания и OCR. Распознавание фотографии может ждать
    # модель до двух минут, и без промежуточного сообщения человек видит молчащего бота и
    # отправляет тот же документ повторно. Финальный ответ ниже придёт при любом штатном исходе,
    # а общий обработчик poll_and_ingest отдельно сообщит о неожиданной ошибке.
    await _notify(
        client,
        token,
        chat_id,
        "📥 Файл получил. Начинаю распознавание — это может занять пару минут. "
        "По результату обязательно напишу.",
    )

    file_id, filename = picked
    file_info = await _call(client, token, "getFile", file_id=file_id)
    file_path = (file_info or {}).get("file_path")
    if not file_path:
        raise TelegramIntakeError("getFile не вернул путь к файлу")
    content = await _download(client, token, str(file_path))

    try:
        rows = await utility_intake.ingest_document(
            session,
            content=content,
            filename=filename or str(file_path).rsplit("/", 1)[-1],
            settings=settings,
            source_label=sender_label(message),
        )
    except utility_intake.UtilityIntakeError as exc:
        await session.rollback()
        await _notify(client, token, chat_id, f"⚠️ {exc}")
        return "refused"

    await session.commit()
    await _notify(client, token, chat_id, _reply_text(rows))
    return "linked" if all(row.status == "linked" for row in rows) else "needs_review"


async def poll_and_ingest(session: AsyncSession, *, settings: Settings) -> dict[str, Any]:
    """Забрать новые сообщения бота и завести по ним документы. Возвращает счётчики прохода.

    Каждое сообщение обрабатывается отдельно и подтверждается независимо от исхода: иначе один
    непонятный файл встал бы в горло очереди навсегда, и следующие документы не дошли бы вовсе.
    """
    global _next_offset

    token = (settings.telegram_intake_bot_token or "").strip()
    if not token:
        return {"status": "not_configured"}

    max_age = max(0, settings.telegram_intake_max_message_age_minutes) * 60
    now_ts = datetime.now(UTC).timestamp()
    # Про отсечку говорим ОДИН раз на чат за проход: при завале в полсотни сообщений полсотни
    # одинаковых уведомлений — это не забота, а спам.
    warned_chats: set[Any] = set()

    result: dict[str, Any] = {"status": "ok", "updates": 0}
    async with _make_client() as client:
        updates = await _call(
            client,
            token,
            "getUpdates",
            offset=_next_offset,
            timeout=settings.telegram_intake_poll_timeout_seconds,
            allowed_updates=["message"],
        )
        for update in updates or []:
            update_id = int(update.get("update_id"))
            message = update.get("message")
            outcome = "skipped"
            if isinstance(message, dict) and is_stale(
                message, max_age_seconds=max_age, now_ts=now_ts
            ):
                # Подтверждаем, но не заводим: документ вчерашний, а его снимок Телеграм уже
                # не отдаст. Молчать нельзя — человек считает, что документ у нас.
                chat_id = (message.get("chat") or {}).get("id")
                if chat_id is not None and chat_id not in warned_chats:
                    warned_chats.add(chat_id)
                    await _notify(
                        client,
                        token,
                        chat_id,
                        "⏳ Пропустил сообщения старше часа — они пришли, пока приёмка была "
                        "выключена. Если документ ещё актуален, пришлите его заново.",
                    )
                logger.info("telegram_intake: сообщение %s старше отсечки — пропущено", update_id)
                outcome = "stale"
            elif isinstance(message, dict):
                try:
                    outcome = await _handle_message(
                        session, client, token=token, settings=settings, message=message
                    )
                except Exception:  # noqa: BLE001 — один сбойный файл не должен рвать проход
                    # Подтверждаем и это сообщение: ссылка на файл в Телеграме живёт около часа,
                    # и повтор через сутки всё равно не скачается — обновление осталось бы в
                    # горле очереди навсегда, а следующие документы не дошли бы вовсе. Потерю
                    # делаем видимой: человеку в чат, подробности в лог.
                    await session.rollback()
                    logger.warning("telegram_intake: обработка сообщения не удалась", exc_info=True)
                    await _notify(
                        client,
                        token,
                        (message.get("chat") or {}).get("id"),
                        "⚠️ Не смог обработать этот файл — пришлите его ещё раз.",
                    )
                    outcome = "failed"
            _next_offset = update_id + 1
            result["updates"] += 1
            result[outcome] = int(result.get(outcome, 0)) + 1
    return result

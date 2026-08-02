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

ЧУЖИХ НЕ СЛУШАЕМ. Имя бота публично, и написать ему может кто угодно. Без белого списка чатов
любой прохожий заводил бы в учёте обязательства, поэтому пустой список означает «никого»: бот
отвечает собеседнику его же chat id, чтобы владелец мог внести его в настройку, и на этом всё.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import EmailInvoiceIntake
from app.services import utility_intake

logger = logging.getLogger(__name__)

__all__ = ["TelegramIntakeError", "allowed_chat_ids", "poll_and_ingest"]

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


def allowed_chat_ids(raw: str) -> frozenset[int]:
    """Разобрать список из настройки. Мусор молча пропускаем — падать из-за опечатки нельзя."""
    ids: set[int] = set()
    for part in (raw or "").replace(";", ",").split(","):
        piece = part.strip()
        if not piece:
            continue
        try:
            ids.add(int(piece))
        except ValueError:
            logger.warning("telegram_intake: в белом списке чатов не число: %r", piece)
    return frozenset(ids)


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
    return f"⚠️ Нужны руки: {reason}.\n   Откройте «Страницу на оплату» → «Разобрать»"


def _reply_text(rows: list[EmailInvoiceIntake]) -> str:
    if not rows:
        return "Ничего не разобрал — откройте «Страницу на оплату»"
    head = "Принял документ" if len(rows) == 1 else f"Принял, документов в файле: {len(rows)}"
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

    allowed = allowed_chat_ids(settings.telegram_intake_allowed_chat_ids)
    if chat_id not in allowed:
        # Не молчим: без ответа человек не поймёт, почему бот его игнорирует, а chat id взять
        # ему больше неоткуда. Документ при этом не заводим.
        await _notify(
            client,
            token,
            chat_id,
            "Этот чат не разрешён для приёмки документов.\n"
            f"Ваш chat id: {chat_id} — добавьте его в настройку "
            "TELEGRAM_INTAKE_ALLOWED_CHAT_IDS.",
        )
        logger.warning("telegram_intake: сообщение из чужого чата %s отклонено", chat_id)
        return "rejected"

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
            if isinstance(message, dict):
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

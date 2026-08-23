"""Приёмка документов через телеграм-бота: что доезжает до учёта и что до человека.

Бот — курьер, а не бухгалтер: всю работу делает ``utility_intake``, и проверять её здесь
незачем (у неё свои тесты). Здесь закреплено другое — то, что ломается именно в курьере:

* у каждой строки есть ОТПРАВИТЕЛЬ. Белого списка нет (решение владельца 02.08.2026), поэтому
  имя приславшего — единственное, что отвечает на вопрос «чей это документ»;
* человек получает ОТВЕТ с суммами. Переслать документ в тишину — то же самое, что потерять
  его: владелец не узнает ни что документ принят, ни какие числа система из него достала;
* долгий OCR видим человеку. Сразу после получения файла бот подтверждает начало работы, а
  затем отдельным сообщением сообщает успех или понятную проблему;
* очередь не встаёт колом. Ссылка на файл в Телеграме живёт около часа, и сбойное сообщение,
  которое не подтвердили, блокировало бы все следующие документы навсегда;
* вчерашний завал не вываливается в учёт. Телеграм держит неподтверждённое сутки и отдаёт
  всё разом при первом обращении — после деплоя это была бы пачка документов, часть которых
  уже проведена руками.

Телеграм подменён транспортом ``httpx.MockTransport``, распознавание — текстом настоящего
акта: проверяем курьера, а не сеть и не зрение модели.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from cp_helpers import make_counterparty
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models import (
    DdsArticle,
    EmailInvoiceIntake,
    Location,
    Organization,
    UtilityAccount,
)
from app.services import telegram_intake, utility_ocr

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "utility"
JPEG = b"\xff\xd8\xff" + b"telegram-photo"
OWNER_CHAT = 4242
TOKEN = "test-token"


class FakeTelegram:
    """Телеграм на подмене: отдаёт заготовленные обновления и копит отправленные ответы."""

    def __init__(self, batches: list[list[dict]], files: dict[str, bytes]) -> None:
        self.batches = batches
        self.files = files
        self.sent: list[dict] = []
        self.offsets: list[int | None] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/getUpdates"):
            body = json.loads(request.content)
            self.offsets.append(body.get("offset"))
            batch = self.batches.pop(0) if self.batches else []
            return httpx.Response(200, json={"ok": True, "result": batch})
        if path.endswith("/getFile"):
            file_id = json.loads(request.content)["file_id"]
            return httpx.Response(
                200, json={"ok": True, "result": {"file_path": f"photos/{file_id}.jpg"}}
            )
        if path.endswith("/sendMessage"):
            self.sent.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {}})
        if "/file/bot" in path:
            name = path.rsplit("/", 1)[-1].removesuffix(".jpg")
            return httpx.Response(200, content=self.files.get(name, JPEG))
        raise AssertionError(f"неожиданный вызов Телеграма: {path}")

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def _photo_update(
    update_id: int,
    *,
    chat_id: int = OWNER_CHAT,
    file_id: str = "act",
    age_seconds: int = 0,
) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(datetime.now(UTC).timestamp()) - age_seconds,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "first_name": "Григорий", "username": "gshokin"},
            "photo": [
                {"file_id": f"{file_id}-small", "file_size": 1000},
                {"file_id": file_id, "file_size": 90000},
            ],
        },
    }


def _text_update(update_id: int, text: str = "привет") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(datetime.now(UTC).timestamp()),
            "chat": {"id": OWNER_CHAT, "type": "private"},
            "text": text,
        },
    }


@pytest.fixture(autouse=True)
def _reset_cursor():
    """Курсор живёт в памяти модуля — между тестами он обязан быть чистым."""
    telegram_intake._next_offset = None
    yield
    telegram_intake._next_offset = None


@pytest.fixture
def settings_with_bot(monkeypatch: pytest.MonkeyPatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_intake_bot_token", TOKEN, raising=False)
    return settings


@pytest.fixture
def ocr_act(monkeypatch: pytest.MonkeyPatch):
    text = (FIXTURE_ROOT / "electricity_real_20260717_actual.txt").read_text(encoding="utf-8")

    async def fake_extract(content, *, mime, settings):  # noqa: ANN001, ARG001
        return text, "vision"

    monkeypatch.setattr(utility_ocr, "extract_text", fake_extract)


async def _flow(session: AsyncSession) -> UtilityAccount:
    organization = await session.scalar(select(Organization).limit(1))
    if organization is None:
        organization = Organization(id=uuid.uuid4(), name="Тепло")
        session.add(organization)
        await session.flush()
    location = Location(id=uuid.uuid4(), organization_id=organization.id, name="Черникова")
    article = DdsArticle(
        id=uuid.uuid4(),
        code=f"art_{uuid.uuid4().hex[:8]}",
        name="Коммунальные платежи",
        movement_type="outflow",
        activity_type="operating",
        location_required=True,
    )
    session.add_all([location, article])
    await session.flush()
    landlord = await make_counterparty(
        session,
        name="Гордеев Виталий Анатольевич",
        inn=f"6143{uuid.uuid4().int % 10**8:08d}",
        cp_type="individual",
        relationship="informal",
    )
    account = UtilityAccount(
        location_id=location.id,
        counterparty_id=landlord.id,
        kind="electricity",
        dds_article_id=article.id,
        started_on=date(2026, 1, 1),
    )
    session.add(account)
    await session.commit()
    return account


async def _intake_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(EmailInvoiceIntake)) or 0)


async def test_forwarded_photo_becomes_payable_row(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    settings_with_bot,
    ocr_act,
) -> None:
    """Переслал фото — строка в очереди оплат, а в чат пришли обе суммы.

    Ответ с числами обязателен: без него владелец не знает ни что документ принят, ни что
    система вычла из расхода зачтённый аванс. Молчащий бот равен потерянному документу.
    """
    telegram = FakeTelegram([[_photo_update(10)]], {"act": JPEG})
    monkeypatch.setattr(telegram_intake, "_make_client", telegram.client)

    async with async_session_factory() as session:
        account = await _flow(session)

        result = await telegram_intake.poll_and_ingest(session, settings=settings_with_bot)

        assert result["updates"] == 1
        assert result.get("linked") == 1
        row = await session.scalar(select(EmailInvoiceIntake))
        assert row is not None
        assert row.status == "linked"
        assert row.mailbox == "photo"
        assert row.utility_account_id == account.id

        started, reply = telegram.sent
        assert started["chat_id"] == OWNER_CHAT
        assert "Файл получил" in started["text"]
        assert "обязательно напишу" in started["text"]
        assert reply["chat_id"] == OWNER_CHAT
        assert "Готово" in reply["text"]
        assert "30402.00" in reply["text"]
        assert "95402.00" in reply["text"], "расход периода обязан быть назван отдельно"


async def test_any_sender_is_accepted_and_remembered(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    settings_with_bot,
    ocr_act,
) -> None:
    """Белого списка нет — принимаем от любого, но помним, кто прислал.

    Владелец отказался от списка 02.08.2026: вести его руками при каждом новом отправителе
    дороже, чем разобрать спорную строку. Взамен отправитель обязан быть записан — иначе у
    документа с чужой суммой не окажется имени, и спрашивать будет не с кого.
    """
    telegram = FakeTelegram([[_photo_update(11, chat_id=999)]], {"act": JPEG})
    monkeypatch.setattr(telegram_intake, "_make_client", telegram.client)

    async with async_session_factory() as session:
        await _flow(session)

        result = await telegram_intake.poll_and_ingest(session, settings=settings_with_bot)

        assert result.get("linked") == 1
        row = await session.scalar(select(EmailInvoiceIntake))
        assert row is not None
        assert row.from_addr is not None
        # Имя и chat id рядом: имена повторяются и меняются, id — нет.
        assert "Григорий" in row.from_addr
        assert "999" in row.from_addr


async def test_text_without_file_is_answered_not_ignored(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    settings_with_bot,
) -> None:
    """Сообщение без файла — не ошибка, а повод объяснить, что от человека нужно."""
    telegram = FakeTelegram([[_text_update(12)]], {})
    monkeypatch.setattr(telegram_intake, "_make_client", telegram.client)

    async with async_session_factory() as session:
        result = await telegram_intake.poll_and_ingest(session, settings=settings_with_bot)

        assert result.get("no_file") == 1
        assert await _intake_count(session) == 0
        (reply,) = telegram.sent
        assert "квитанции" in reply["text"]


async def test_same_photo_twice_creates_one_row(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    settings_with_bot,
    ocr_act,
) -> None:
    """Переслал тот же файл дважды — вторая строка не появляется, и об этом говорят вслух."""
    telegram = FakeTelegram([[_photo_update(13), _photo_update(14)]], {"act": JPEG})
    monkeypatch.setattr(telegram_intake, "_make_client", telegram.client)

    async with async_session_factory() as session:
        await _flow(session)

        result = await telegram_intake.poll_and_ingest(session, settings=settings_with_bot)

        assert result["updates"] == 2
        assert await _intake_count(session) == 1
        assert "уже загружали" in telegram.sent[-1]["text"]


async def test_cursor_moves_past_broken_message(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    settings_with_bot,
    ocr_act,
) -> None:
    """Сбойное сообщение подтверждается, а не встаёт в горле очереди.

    Ссылка на файл в Телеграме живёт около часа: сообщение, которое не подтвердили, вернулось
    бы в следующем проходе и сломалось снова — и все документы после него не дошли бы никогда.
    Поэтому курсор двигается, а потеря становится видимой: человеку в чат.
    """
    broken = _photo_update(20, file_id="boom")

    class Failing(FakeTelegram):
        def handler(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/getFile"):
                file_id = json.loads(request.content)["file_id"]
                if file_id == "boom":
                    return httpx.Response(400, json={"ok": False, "description": "file is gone"})
            return super().handler(request)

    telegram = Failing([[broken, _photo_update(21)]], {"act": JPEG})
    monkeypatch.setattr(telegram_intake, "_make_client", telegram.client)

    async with async_session_factory() as session:
        await _flow(session)

        result = await telegram_intake.poll_and_ingest(session, settings=settings_with_bot)

        assert result.get("failed") == 1
        # Следующее сообщение того же прохода обработано, а не пропущено.
        assert result.get("linked") == 1
        assert await _intake_count(session) == 1
        texts = [message["text"] for message in telegram.sent]
        assert "Файл получил" in texts[0]
        assert any("пришлите его ещё раз" in text for text in texts)
        # Курсор ушёл за оба сообщения: следующий проход попросит уже 22.
        assert telegram_intake._next_offset == 22


async def test_ocr_problem_is_reported_after_processing_started(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    settings_with_bot,
) -> None:
    """Если OCR не сработал, бот не зависает на «начал», а честно завершает разговор ошибкой."""

    async def failed_ocr(content, *, mime, settings):  # noqa: ANN001, ARG001
        return None, "vision_failed"

    monkeypatch.setattr(utility_ocr, "extract_text", failed_ocr)
    telegram = FakeTelegram([[_photo_update(22)]], {"act": JPEG})
    monkeypatch.setattr(telegram_intake, "_make_client", telegram.client)

    async with async_session_factory() as session:
        result = await telegram_intake.poll_and_ingest(session, settings=settings_with_bot)

        assert result.get("needs_review") == 1
        started, finished = telegram.sent
        assert "Файл получил" in started["text"]
        assert "Обработку закончил" in finished["text"]
        assert "Не удалось прочитать текст со снимка" in finished["text"]


def test_bot_token_never_reaches_the_log() -> None:
    """Токен из URL в журнал не попадает.

    httpx пишет URL целиком на уровне INFO, а токен у Телеграма стоит в пути запроса. В проде
    логи идут на INFO, опрос тикает каждые полминуты — секрет оказался бы в журнале сотни раз в
    сутки. Один раз на этом уже пришлось отзывать бота, поэтому проверка не про аккуратность,
    а про повторение известной ошибки.
    """
    # Проверяем две вещи по отдельности: что фильтр ПОВЕШЕН на нужные логгеры и что он ЧИСТИТ.
    # Гонять настоящую запись через logging здесь нельзя: под pytest вывод логов приглушён, и
    # обработчик не увидит ничего — тест был бы зелёным ровно потому, что молчит всё.
    for name in ("httpx", "httpcore"):
        assert any(
            isinstance(item, telegram_intake._RedactBotToken)
            for item in logging.getLogger(name).filters
        ), f"фильтр не повешен на логгер {name}"

    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg='HTTP Request: POST https://api.telegram.org/bot%s/getUpdates "HTTP/1.1 200 OK"',
        args=("8606922023:AAGsLhU4LRwip2WRmY1dh9d_KuMxo7HSdN8",),
        exc_info=None,
    )
    telegram_intake._RedactBotToken().filter(record)

    text = record.getMessage()
    assert "AAGsLhU4LRwip2WRmY1dh9d_KuMxo7HSdN8" not in text
    # Номер бота оставляем: он не секрет, а по нему видно, о каком боте речь.
    assert "/bot8606922023:***" in text


async def test_without_token_nothing_happens(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Бот не настроен — проход молча ничего не делает, а не падает каждые полминуты.

    Токен гасим явно: на стенде он приходит из окружения контейнера, и без этого тест ходил бы
    в живой Телеграм — то есть проверял бы сеть вместо выключателя.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_intake_bot_token", None, raising=False)
    async with async_session_factory() as session:
        result = await telegram_intake.poll_and_ingest(session, settings=settings)
    assert result == {"status": "not_configured"}


async def test_yesterdays_backlog_is_skipped_with_one_notice(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    settings_with_bot,
    ocr_act,
) -> None:
    """Вчерашние сообщения не заводят документов — но человек об этом узнаёт один раз.

    После деплоя Телеграм отдаёт всё неподтверждённое за сутки. Провести эту пачку значило бы
    завести документы, часть которых уже внесена руками, а снимки к ним Телеграм и не отдаст:
    ссылка на файл живёт около часа. Молчать тоже нельзя — человек считает, что документ у нас.
    Уведомление ОДНО на чат: полсотни одинаковых сообщений — это не забота, а спам.
    """
    stale = [
        _photo_update(30, age_seconds=8 * 3600),
        _photo_update(31, age_seconds=7 * 3600, file_id="act2"),
    ]
    telegram = FakeTelegram([[*stale, _photo_update(32, file_id="act3")]], {"act": JPEG})
    monkeypatch.setattr(telegram_intake, "_make_client", telegram.client)

    async with async_session_factory() as session:
        await _flow(session)

        result = await telegram_intake.poll_and_ingest(session, settings=settings_with_bot)

        assert result.get("stale") == 2
        # Свежее сообщение того же прохода обработано — отсечка режет по возрасту, а не подряд.
        assert result.get("linked") == 1
        assert await _intake_count(session) == 1
        notices = [msg for msg in telegram.sent if "старше часа" in msg["text"]]
        assert len(notices) == 1, "уведомление об отсечке — одно на чат за проход"
        # Курсор ушёл за все три: иначе вчерашний завал возвращался бы каждые полминуты.
        assert telegram_intake._next_offset == 33


async def test_cutoff_can_be_switched_off(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    settings_with_bot,
    ocr_act,
) -> None:
    """Ноль в настройке — отсечки нет: старое сообщение берётся в работу как обычное."""
    monkeypatch.setattr(
        settings_with_bot, "telegram_intake_max_message_age_minutes", 0, raising=False
    )
    telegram = FakeTelegram([[_photo_update(40, age_seconds=10 * 3600)]], {"act": JPEG})
    monkeypatch.setattr(telegram_intake, "_make_client", telegram.client)

    async with async_session_factory() as session:
        await _flow(session)

        result = await telegram_intake.poll_and_ingest(session, settings=settings_with_bot)

        assert result.get("linked") == 1

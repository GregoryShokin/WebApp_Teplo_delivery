"""Приёмка документов через телеграм-бота: что доезжает до учёта и что до человека.

Бот — курьер, а не бухгалтер: всю работу делает ``utility_intake``, и проверять её здесь
незачем (у неё свои тесты). Здесь закреплено другое — то, что ломается именно в курьере:

* чужому не отвечают документом. Имя бота публично, написать ему может кто угодно, и без
  белого списка чатов посторонний заводил бы в учёте обязательства;
* человек получает ОТВЕТ с суммами. Переслать документ в тишину — то же самое, что потерять
  его: владелец не узнает ни что документ принят, ни какие числа система из него достала;
* очередь не встаёт колом. Ссылка на файл в Телеграме живёт около часа, и сбойное сообщение,
  которое не подтвердили, блокировало бы все следующие документы навсегда.

Телеграм подменён транспортом ``httpx.MockTransport``, распознавание — текстом настоящего
акта: проверяем курьера, а не сеть и не зрение модели.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
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


def _photo_update(update_id: int, *, chat_id: int = OWNER_CHAT, file_id: str = "act") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": chat_id, "type": "private"},
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
    monkeypatch.setattr(
        settings, "telegram_intake_allowed_chat_ids", str(OWNER_CHAT), raising=False
    )
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

        (reply,) = telegram.sent
        assert reply["chat_id"] == OWNER_CHAT
        assert "30402.00" in reply["text"]
        assert "95402.00" in reply["text"], "расход периода обязан быть назван отдельно"


async def test_stranger_gets_only_his_chat_id(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    settings_with_bot,
    ocr_act,
) -> None:
    """Чужой чат документа не заводит. Имя бота публично — иначе учёт наполнял бы прохожий."""
    telegram = FakeTelegram([[_photo_update(11, chat_id=999)]], {"act": JPEG})
    monkeypatch.setattr(telegram_intake, "_make_client", telegram.client)

    async with async_session_factory() as session:
        await _flow(session)

        result = await telegram_intake.poll_and_ingest(session, settings=settings_with_bot)

        assert result.get("rejected") == 1
        assert await _intake_count(session) == 0
        (reply,) = telegram.sent
        # Свой chat id человеку взять больше неоткуда — без него он не сможет попросить доступ.
        assert "999" in reply["text"]


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
        assert "уже загружали" in telegram.sent[1]["text"]


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
        assert "пришлите его ещё раз" in telegram.sent[0]["text"]
        # Курсор ушёл за оба сообщения: следующий проход попросит уже 22.
        assert telegram_intake._next_offset == 22


async def test_without_token_nothing_happens(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Бот не настроен — проход молча ничего не делает, а не падает каждые полминуты."""
    settings = get_settings()
    async with async_session_factory() as session:
        result = await telegram_intake.poll_and_ingest(session, settings=settings)
    assert result == {"status": "not_configured"}

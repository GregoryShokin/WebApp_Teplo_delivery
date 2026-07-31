"""Предзаполнение реквизитов в окне разбора счёта на оплату.

Окно узнавало контрагента (по ИНН из PDF, по адресу отправителя или по точному имени),
но пять полей платёжки оставляло пустыми: единственным источником для них было
распознанное вложение. Реквизиты карточки — те самые, которыми платёж и уйдёт в банк
(``create_payment_draft_for_invoices`` берёт ``profile.requisites``) — во фронт не
попадали вовсе, и оператор перебивал их с бумажки.

Здесь закреплено две вещи: карточка отдаётся окну отдельным лёгким ответом (контрагента
в окне можно переключить, поэтому по выбранному id, а не только по сматченному), а правки
оператора больше не затирают распознанное — иначе подставленная карточка при первом же
подтверждении становилась бы «реквизитами из счёта» и сравнивать было бы не с чем.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from cp_helpers import admin_headers, make_counterparty
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import EmailInvoiceIntake
from app.services.counterparty_requisites_history import search_history_requisites

# Счёт и БИК согласованы по контрольному разряду: ``set_requisites`` не даёт пометить
# проверенными реквизиты, которые банк отклонит (см. payee_account_error).
CARD_REQUISITES = {
    "recipientName": 'ООО "АЛЬЯНС ЮГ"',
    "inn": "6143059250",
    "kpp": "614301001",
    "bankAcnt": "40702810400000012349",
    "bankBik": "044525225",
    "recipientCorrAccountNumber": "30101810400000000225",
}


def admin_headers_sync(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    return asyncio.run(admin_headers(session_factory))


async def _make_intake(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID | None = None,
    recognition: dict[str, object] | None = None,
    status: str = "needs_review",
) -> EmailInvoiceIntake:
    intake = EmailInvoiceIntake(
        mailbox="corporate",
        from_addr="buh@alliance-yug.ru",
        attachment_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        status=status,
        received_at=datetime(2026, 7, 30, tzinfo=UTC),
        counterparty_id=counterparty_id,
        recognition=recognition if recognition is not None else {"amount": "12000.00"},
    )
    session.add(intake)
    await session.flush()
    return intake


def test_card_requisites_endpoint_feeds_the_review_dialog(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Счёт без распознанных реквизитов: окну нужны реквизиты карточки, иначе форма пуста."""
    holder: dict[str, uuid.UUID] = {}

    async def seed() -> None:
        async with async_session_factory() as session:
            counterparty = await make_counterparty(
                session,
                name='ООО "АЛЬЯНС ЮГ"',
                inn="6143059250",
                requisites=CARD_REQUISITES,
                requisites_verified=True,
            )
            holder["cp"] = counterparty.id
            await session.commit()

    asyncio.run(seed())
    headers = admin_headers_sync(async_session_factory)

    response = client.get(
        f"/api/v1/payment-page/counterparties/{holder['cp']}/requisites", headers=headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["requisites"] == CARD_REQUISITES
    assert body["requisites_verified"] is True
    assert body["inn"] == "6143059250"


def test_card_requisites_empty_for_counterparty_without_profile(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Заглушка из письма (карточка ещё не заполнена) — пустой набор, а не 500."""
    holder: dict[str, uuid.UUID] = {}

    async def seed() -> None:
        async with async_session_factory() as session:
            counterparty = await make_counterparty(session, name="Новый поставщик", inn=None)
            holder["cp"] = counterparty.id
            await session.commit()

    asyncio.run(seed())
    headers = admin_headers_sync(async_session_factory)

    response = client.get(
        f"/api/v1/payment-page/counterparties/{holder['cp']}/requisites", headers=headers
    )

    assert response.status_code == 200
    assert response.json()["requisites"] == {}
    assert response.json()["requisites_verified"] is False


def test_card_requisites_unknown_counterparty_is_404(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    headers = admin_headers_sync(async_session_factory)
    response = client.get(
        f"/api/v1/payment-page/counterparties/{uuid.uuid4()}/requisites", headers=headers
    )
    assert response.status_code == 404


def test_card_requisites_requires_authorization(client: TestClient) -> None:
    response = client.get(f"/api/v1/payment-page/counterparties/{uuid.uuid4()}/requisites")
    assert response.status_code in (401, 403)


def test_review_keeps_recognized_requisites_apart_from_operator_edits(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Подтверждение с правками не должно выдавать данные карточки за данные счёта.

    Форма предзаполняется карточкой, когда PDF реквизитов не дал. Если бы правки писались
    поверх ``recognition["requisites"]``, то после первого же подтверждения счёт «получал»
    бы реквизиты, которых в нём не было: подпись источника в окне начала бы врать, а сверка
    «в счёте другой расчётный счёт, чем в карточке» потеряла бы точку отсчёта.
    """
    holder: dict[str, uuid.UUID] = {}
    recognized = {"recipientName": 'ООО "АЛЬЯНС ЮГ"', "inn": "6143059250"}

    async def seed() -> None:
        async with async_session_factory() as session:
            counterparty = await make_counterparty(
                session, name='ООО "АЛЬЯНС ЮГ"', inn="6143059250"
            )
            holder["cp"] = counterparty.id
            intake = await _make_intake(
                session,
                counterparty_id=counterparty.id,
                recognition={"amount": "12000.00", "requisites": dict(recognized)},
            )
            holder["intake"] = intake.id
            await session.commit()

    asyncio.run(seed())
    headers = admin_headers_sync(async_session_factory)

    response = client.post(
        f"/api/v1/payment-page/intakes/{holder['intake']}/confirm",
        json={
            "counterparty_id": str(holder["cp"]),
            "amount": "12000.00",
            "requisites": CARD_REQUISITES,
            "apply_requisites": True,
        },
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    # Распознанное осталось тем, что было в PDF…
    assert body["requisites"] == recognized
    # …а правки оператора живут отдельно и предзаполнят окно при повторном заходе.
    assert body["reviewed_requisites"] == CARD_REQUISITES
    # Карточка при этом заполнена и помечена проверенной — платёж уйдёт по ней.
    card = client.get(
        f"/api/v1/payment-page/counterparties/{holder['cp']}/requisites", headers=headers
    ).json()
    assert card["requisites"] == CARD_REQUISITES
    assert card["requisites_verified"] is True


def test_history_prefers_operator_corrected_requisites(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Кандидат из почты собирается по исправленному оператором, сырое — фолбэк.

    Правил оператор ровно потому, что парсер ошибся: БИК из PDF вытащен неверно, а ИНН
    вовсе не распознан. Подсказывать другим карточкам заведомо битое значение незачем.
    """

    async def scenario() -> None:
        async with async_session_factory() as session:
            await _make_intake(
                session,
                recognition={
                    "amount": "12000.00",
                    "requisites": {
                        "recipientName": 'ООО "АЛЬЯНС ЮГ"',
                        "bankAcnt": "40702810300000077777",
                        "bankBik": "044525974",
                    },
                    "requisites_reviewed": {
                        "recipientName": 'ООО "АЛЬЯНС ЮГ"',
                        "inn": "6143059250",
                        "bankAcnt": "40702810300000077777",
                        "bankBik": "046015602",
                        "recipientCorrAccountNumber": "30101810600000000602",
                    },
                },
                status="linked",
            )
            await session.commit()

            # Ищется по исправленному ИНН, которого в сыром распознавании не было вовсе.
            candidates = await search_history_requisites(session, "6143059250")

            assert len(candidates) == 1
            assert candidates[0].source == "email"
            assert candidates[0].requisites["bankBik"] == "046015602"
            assert candidates[0].missing == []

    asyncio.run(scenario())


def test_history_falls_back_to_raw_recognition(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Неразобранный счёт (оператор до него не дошёл) остаётся видимым для поиска."""

    async def scenario() -> None:
        async with async_session_factory() as session:
            await _make_intake(
                session,
                recognition={
                    "amount": "9000.00",
                    "requisites": {
                        "recipientName": "ИП Егиазарян Гарик Ваграмович",
                        "inn": "614307902094",
                        "bankAcnt": "40802810100002438573",
                        "bankBik": "046015602",
                        "recipientCorrAccountNumber": "30101810600000000602",
                    },
                },
            )
            await session.commit()

            candidates = await search_history_requisites(session, "614307902094")

            assert len(candidates) == 1
            assert candidates[0].requisites["bankAcnt"] == "40802810100002438573"

    asyncio.run(scenario())

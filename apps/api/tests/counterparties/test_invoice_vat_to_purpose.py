"""Доля НДС из счёта — до назначения платежа.

Назначение платежа обязано называть сумму налога («в том числе НДС 22% — 1 439,90») или прямо
говорить «Без НДС»: это читают банк и налоговая. Собирать её умели давно
(``counterparty_payments._vat_suffix``), но брать было неоткуда — распознавание счетов налог не
извлекало вовсе, и КАЖДАЯ платёжка по счёту из почты уходила с «Без НДС.» независимо от того,
что стояло в бумаге.

Здесь закреплён весь путь: распознанный налог → поля накладной → текст платёжки, и отдельно —
правки оператора, потому что окончательное слово о налоге за человеком с документом в руках.
Решение владельца 07.08.2026: нераспознанный налог отправку в банк НЕ блокирует (в платёжке
будет «Без НДС.»), но оператор видит поле в окне разбора и в окне отправки.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from cp_helpers import admin_headers, make_counterparty
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import EmailInvoiceIntake, SupplierInvoice
from app.services.counterparty_payments import create_payment_draft_for_invoices

SUPPLIER_REQUISITES = {
    "recipientName": 'ООО "СДЭК-СЛАВЯНСК"',
    "inn": "2370006152",
    "bankAcnt": "40702810400000012349",
    "bankBik": "044525225",
    "recipientCorrAccountNumber": "30101810400000000225",
}

RECOGNIZED_WITH_VAT = {
    "amount": "7984.90",
    "invoice_number": "СКБ-0437096",
    "invoice_date": "2026-07-19",
    "document_kind": "invoice",
    "vat_mode": "included",
    "vat_rate": "22",
    "vat_amount": "1439.90",
    "confidence": 0.9,
}


async def _intake(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    recognition: dict[str, object],
    status: str = "needs_review",
) -> EmailInvoiceIntake:
    intake = EmailInvoiceIntake(
        mailbox="corporate",
        from_addr="buh@cdek.ru",
        attachment_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        status=status,
        received_at=datetime(2026, 7, 19, tzinfo=UTC),
        counterparty_id=counterparty_id,
        recognition=recognition,
    )
    session.add(intake)
    await session.flush()
    return intake


def _headers(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    return asyncio.run(admin_headers(session_factory))


def test_recognized_vat_reaches_the_bank_purpose(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Счёт из почты с распознанным налогом: платёжка называет ставку и сумму НДС."""
    holder: dict[str, uuid.UUID] = {}

    async def seed() -> None:
        async with async_session_factory() as session:
            supplier = await make_counterparty(
                session,
                name='ООО "СДЭК-СЛАВЯНСК"',
                inn="2370006152",
                requisites=SUPPLIER_REQUISITES,
                requisites_verified=True,
            )
            intake = await _intake(
                session, counterparty_id=supplier.id, recognition=dict(RECOGNIZED_WITH_VAT)
            )
            holder["intake"] = intake.id
            holder["cp"] = supplier.id
            await session.commit()

    asyncio.run(seed())

    response = client.post(
        f"/api/v1/payment-page/intakes/{holder['intake']}/confirm",
        json={"counterparty_id": str(holder["cp"])},
        headers=_headers(async_session_factory),
    )
    assert response.status_code == 200

    async def check() -> None:
        async with async_session_factory() as session:
            invoice = await session.scalar(
                select(SupplierInvoice).where(SupplierInvoice.counterparty_id == holder["cp"])
            )
            assert invoice is not None
            assert invoice.vat_total == Decimal("1439.90")
            assert invoice.vat_breakdown == {"22": "1439.90"}

            draft = await create_payment_draft_for_invoices(
                session, invoice_ids=[invoice.id], actor_user_id=None
            )
            assert "В т.ч. НДС: 22% - 1439,90 руб." in draft.payload["paymentPurpose"]

    asyncio.run(check())


def test_operator_fills_in_the_vat_that_was_not_recognized(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Налог не распознан — оператор вписывает его с бумаги, и в банк уходит уже правда.

    Это и есть страховка выбранного решения: пустой налог отправку не блокирует, поэтому
    единственная защита от ложного «Без НДС.» — то, что человек видит поле и может его
    заполнить в окне разбора и в окне отправки.
    """
    holder: dict[str, uuid.UUID] = {}

    async def seed() -> None:
        async with async_session_factory() as session:
            supplier = await make_counterparty(
                session,
                name='ООО "СДЭК-СЛАВЯНСК"',
                inn="2370006152",
                requisites=SUPPLIER_REQUISITES,
                requisites_verified=True,
            )
            intake = await _intake(
                session,
                counterparty_id=supplier.id,
                recognition={
                    "amount": "7984.90",
                    "invoice_number": "СКБ-0437096",
                    "document_kind": "invoice",
                    "vat_mode": "",
                },
            )
            holder["intake"] = intake.id
            holder["cp"] = supplier.id
            await session.commit()

    asyncio.run(seed())
    headers = _headers(async_session_factory)

    first = client.get(f"/api/v1/payment-page/intakes/{holder['intake']}", headers=headers).json()
    # Окно должно отличать «не распознан» от «в счёте без НДС» — иначе человек не поймёт,
    # что цифру надо перебить.
    assert first["vat_mode"] == ""
    assert first["vat_amount"] is None

    confirmed = client.post(
        f"/api/v1/payment-page/intakes/{holder['intake']}/confirm",
        json={
            "counterparty_id": str(holder["cp"]),
            "vat_amount": "1439.90",
            "vat_rate": "22",
        },
        headers=headers,
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["vat_mode"] == "included"
    assert confirmed.json()["vat_amount"] == "1439.90"

    async def check() -> None:
        async with async_session_factory() as session:
            invoice = await session.scalar(
                select(SupplierInvoice).where(SupplierInvoice.counterparty_id == holder["cp"])
            )
            assert invoice is not None
            draft = await create_payment_draft_for_invoices(
                session, invoice_ids=[invoice.id], actor_user_id=None
            )
            assert "В т.ч. НДС: 22% - 1439,90 руб." in draft.payload["paymentPurpose"]

    asyncio.run(check())


def test_operator_corrects_vat_on_an_already_created_invoice(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Правка налога у ЗАВЕДЁННОГО счёта доходит до накладной — это путь окна отправки в банк.

    Окно отправки правит счёт тем же ``/confirm``, а назначение платежа собирается из полей
    накладной, а не из распознанного. Без синхронизации правка оставалась бы в intake и до
    платёжки не доезжала — то есть человек «поправил», а банк получил прежнее.
    """
    holder: dict[str, uuid.UUID] = {}

    async def seed() -> None:
        async with async_session_factory() as session:
            supplier = await make_counterparty(
                session,
                name='ООО "СДЭК-СЛАВЯНСК"',
                inn="2370006152",
                requisites=SUPPLIER_REQUISITES,
                requisites_verified=True,
            )
            intake = await _intake(
                session, counterparty_id=supplier.id, recognition=dict(RECOGNIZED_WITH_VAT)
            )
            holder["intake"] = intake.id
            holder["cp"] = supplier.id
            await session.commit()

    asyncio.run(seed())
    headers = _headers(async_session_factory)

    client.post(
        f"/api/v1/payment-page/intakes/{holder['intake']}/confirm",
        json={"counterparty_id": str(holder["cp"])},
        headers=headers,
    )
    # Второй заход по тому же счёту: распознали 1 439,90, в бумаге 1 440,00.
    again = client.post(
        f"/api/v1/payment-page/intakes/{holder['intake']}/confirm",
        json={"vat_amount": "1440.00", "vat_rate": "22"},
        headers=headers,
    )
    assert again.status_code == 200

    async def check() -> None:
        async with async_session_factory() as session:
            invoice = await session.scalar(
                select(SupplierInvoice).where(SupplierInvoice.counterparty_id == holder["cp"])
            )
            assert invoice is not None
            assert invoice.vat_total == Decimal("1440.00")
            draft = await create_payment_draft_for_invoices(
                session, invoice_ids=[invoice.id], actor_user_id=None
            )
            assert "В т.ч. НДС: 22% - 1440,00 руб." in draft.payload["paymentPurpose"]

    asyncio.run(check())


def test_vat_above_the_invoice_amount_is_rejected(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Налог больше суммы счёта — отказ, а не молчаливое сохранение.

    Опечатка в поле НДС стоит дороже обычной: она уедет в текст платёжного поручения, где её
    прочитает банк.
    """
    holder: dict[str, uuid.UUID] = {}

    async def seed() -> None:
        async with async_session_factory() as session:
            supplier = await make_counterparty(
                session,
                name='ООО "СДЭК-СЛАВЯНСК"',
                inn="2370006152",
                requisites=SUPPLIER_REQUISITES,
                requisites_verified=True,
            )
            intake = await _intake(
                session, counterparty_id=supplier.id, recognition=dict(RECOGNIZED_WITH_VAT)
            )
            holder["intake"] = intake.id
            holder["cp"] = supplier.id
            await session.commit()

    asyncio.run(seed())

    response = client.post(
        f"/api/v1/payment-page/intakes/{holder['intake']}/confirm",
        json={"counterparty_id": str(holder["cp"]), "vat_amount": "9000.00"},
        headers=_headers(async_session_factory),
    )

    assert response.status_code == 409
    assert "НДС" in response.json()["detail"]


def test_empty_vat_from_the_operator_means_no_vat(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Оператор стёр сумму — это утверждение «налога в счёте нет», и платёжка так и скажет.

    Различие с «не трогали поле» существенно: у нетронутого налога остаётся распознанное
    значение, у стёртого — режим 'none', то есть осознанное «Без НДС.» от человека.
    """
    holder: dict[str, uuid.UUID] = {}

    async def seed() -> None:
        async with async_session_factory() as session:
            supplier = await make_counterparty(
                session,
                name='ООО "СДЭК-СЛАВЯНСК"',
                inn="2370006152",
                requisites=SUPPLIER_REQUISITES,
                requisites_verified=True,
            )
            intake = await _intake(
                session, counterparty_id=supplier.id, recognition=dict(RECOGNIZED_WITH_VAT)
            )
            holder["intake"] = intake.id
            holder["cp"] = supplier.id
            await session.commit()

    asyncio.run(seed())

    response = client.post(
        f"/api/v1/payment-page/intakes/{holder['intake']}/confirm",
        json={"counterparty_id": str(holder["cp"]), "vat_amount": ""},
        headers=_headers(async_session_factory),
    )
    assert response.status_code == 200
    assert response.json()["vat_mode"] == "none"

    async def check() -> None:
        async with async_session_factory() as session:
            invoice = await session.scalar(
                select(SupplierInvoice).where(SupplierInvoice.counterparty_id == holder["cp"])
            )
            assert invoice is not None
            assert invoice.vat_total == Decimal("0.00")
            draft = await create_payment_draft_for_invoices(
                session, invoice_ids=[invoice.id], actor_user_id=None
            )
            assert "Без НДС." in draft.payload["paymentPurpose"]

    asyncio.run(check())

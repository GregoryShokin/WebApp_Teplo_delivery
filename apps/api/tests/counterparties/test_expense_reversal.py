"""Откат признанного расхода: сумма уходит из прибыли и возвращается в дебиторку.

Признание не бывает безошибочным: период указали шире, чем услуга оказана, контрагент вернул
часть денег, лицензию отключили в середине месяца. Откат — способ поправить это, не трогая
базу руками, и он частичный: признали 3 000 ₽, откатили 1 000 → в расходе 2 000, а 1 000 снова
«нам должны закрыть документами или вернуть».

Проверяется главное: деньги никуда не движутся, меняется только то, чем платёж считается, —
и признание по документу контрагента откатить нельзя, там сумму меняет корректировочный
документ, а не наше решение.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

import pytest
from cp_helpers import (
    admin_headers,
    headers_for,
    make_counterparty,
    make_expense_article,
    make_invoice,
)
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    SupplierExpenseAccrual,
    SupplierExpenseReversal,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services import supplier_service_periods as periods

BASE = "/api/v1/accounting/suppliers"

pytestmark = pytest.mark.usefixtures("migrated_db")


def _admin(factory) -> dict[str, str]:
    return asyncio.run(admin_headers(factory))


async def _recognized_payment(
    session: AsyncSession, *, name: str, inn: str, amount: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """Платёж, признанный расходом одного месяца: то, что откатывают."""
    cp = await make_counterparty(session, name=name, inn=inn)
    article = await make_expense_article(session, code=f"REV-{inn}", name=f"Услуги {name}")
    prepayment = SupplierPrepayment(
        counterparty_id=cp.id,
        kind="subscription",
        amount=Decimal(amount),
        amount_settled=Decimal("0.00"),
        status="open",
        article_id=article.id,
    )
    session.add(prepayment)
    await session.commit()
    return cp.id, prepayment.id


def test_partial_reversal_returns_money_to_receivable(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Откат 1 000 из 3 000: в расходе остаётся 2 000, дебиторка растёт на 1 000."""

    async def seed() -> uuid.UUID:
        async with async_session_factory() as session:
            _, prepayment_id = await _recognized_payment(
                session, name="Откат Частичный", inn="6155000601", amount="3000.00"
            )
            return prepayment_id

    prepayment_id = asyncio.run(seed())
    headers = _admin(async_session_factory)
    recognized = client.post(
        f"{BASE}/prepayments/{prepayment_id}/recognize",
        headers=headers,
        json={"service_period_start": "2026-06-01", "service_period_end": "2026-06-30"},
    )
    assert recognized.status_code == 200, recognized.text

    async def accrual_id() -> uuid.UUID:
        async with async_session_factory() as session:
            row = await session.scalar(
                select(SupplierExpenseAccrual)
                .join(SupplierInvoice, SupplierInvoice.id == SupplierExpenseAccrual.invoice_id)
                .where(SupplierInvoice.external_id.like(f"self:{prepayment_id}:%"))
            )
            return row.id

    accrual = asyncio.run(accrual_id())
    response = client.post(
        f"{BASE}/accruals/{accrual}/reverse",
        headers=headers,
        json={"amount": 1000, "reason": "Услуга оказана только половину месяца"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["reversed_amount"] == 1000.0
    assert payload["amount_left"] == 2000.0
    assert payload["fully_cancelled"] is False

    async def check() -> None:
        async with async_session_factory() as session:
            row = await session.get(SupplierExpenseAccrual, accrual)
            assert row.amount == Decimal("2000.00")
            assert row.status == "recognized"

            prepayment = await session.get(SupplierPrepayment, prepayment_id)
            # Тысяча вернулась в дебиторку: платёж снова ждёт закрытия на эту сумму.
            assert prepayment.amount_settled == Decimal("2000.00")
            assert prepayment.status == "partially_settled"

            journal = await session.scalar(
                select(SupplierExpenseReversal).where(
                    SupplierExpenseReversal.accrual_id == accrual
                )
            )
            assert journal.amount == Decimal("1000.00")
            assert journal.amount_before == Decimal("3000.00")
            assert journal.recognition_month == date(2026, 6, 1)
            assert "половину месяца" in journal.reason

    asyncio.run(check())


def test_full_reversal_cancels_the_expense_and_returns_the_money(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Откат на всю сумму отменяет расход, а деньги возвращает в дебиторку.

    Ключ идемпотентности самоакта при этом остаётся ЗАНЯТЫМ — он и служит отметкой «месяц
    откачен вручную». Пока ключ освобождался, ночная джоба признавала месяц заново уже
    следующей ночью (см. test_full_reversal_survives_nightly_job).
    """

    async def seed() -> uuid.UUID:
        async with async_session_factory() as session:
            _, prepayment_id = await _recognized_payment(
                session, name="Откат Полный", inn="6155000602", amount="4000.00"
            )
            return prepayment_id

    prepayment_id = asyncio.run(seed())
    headers = _admin(async_session_factory)
    client.post(
        f"{BASE}/prepayments/{prepayment_id}/recognize",
        headers=headers,
        json={"service_period_start": "2026-06-01", "service_period_end": "2026-06-30"},
    )

    async def accrual_id() -> uuid.UUID:
        async with async_session_factory() as session:
            row = await session.scalar(
                select(SupplierExpenseAccrual)
                .join(SupplierInvoice, SupplierInvoice.id == SupplierExpenseAccrual.invoice_id)
                .where(SupplierInvoice.external_id.like(f"self:{prepayment_id}:%"))
            )
            return row.id

    accrual = asyncio.run(accrual_id())
    response = client.post(
        f"{BASE}/accruals/{accrual}/reverse",
        headers=headers,
        json={"amount": 4000, "reason": "Услуга не оказана, деньги возвращают"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["fully_cancelled"] is True

    async def check() -> None:
        async with async_session_factory() as session:
            row = await session.get(SupplierExpenseAccrual, accrual)
            assert row.status == "cancelled"
            prepayment = await session.get(SupplierPrepayment, prepayment_id)
            assert prepayment.amount_settled == Decimal("0.00")
            assert prepayment.status == "open"

    asyncio.run(check())


def test_document_backed_expense_cannot_be_reversed(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Расход по УПД контрагента откатить нельзя — его меняет корректировочный документ."""

    async def seed() -> uuid.UUID:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Откат Документ", inn="6155000603")
            invoice = await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="5000.00",
                doc_kind="closing",
                invoice_date=date(2026, 6, 30),
            )
            await session.commit()
            accrual = await periods.set_invoice_service_period(
                session,
                invoice=invoice,
                start=date(2026, 6, 1),
                end=date(2026, 6, 30),
                actor_user_id=None,
            )
            return accrual.id

    accrual_id = asyncio.run(seed())
    response = client.post(
        f"{BASE}/accruals/{accrual_id}/reverse",
        headers=_admin(async_session_factory),
        json={"amount": 1000, "reason": "Хотим уменьшить"},
    )
    assert response.status_code == 409
    assert "корректировочный" in response.json()["detail"]


def test_reversal_needs_its_own_permission(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Права редактировать взаиморасчёты мало: откат меняет прибыль закрытого месяца."""

    async def seed() -> uuid.UUID:
        async with async_session_factory() as session:
            _, prepayment_id = await _recognized_payment(
                session, name="Откат Права", inn="6155000604", amount="2000.00"
            )
            return prepayment_id

    prepayment_id = asyncio.run(seed())
    # office_manager правит взаиморасчёты, но прибыль закрытого месяца ему не двигать.
    limited_headers = asyncio.run(
        headers_for(async_session_factory, "reversal-denied@teplo.local", ["office_manager"])
    )
    client.post(
        f"{BASE}/prepayments/{prepayment_id}/recognize",
        headers=_admin(async_session_factory),
        json={"service_period_start": "2026-06-01", "service_period_end": "2026-06-30"},
    )

    async def accrual_id() -> uuid.UUID:
        async with async_session_factory() as session:
            row = await session.scalar(
                select(SupplierExpenseAccrual)
                .join(SupplierInvoice, SupplierInvoice.id == SupplierExpenseAccrual.invoice_id)
                .where(SupplierInvoice.external_id.like(f"self:{prepayment_id}:%"))
            )
            return row.id

    accrual = asyncio.run(accrual_id())
    denied = client.post(
        f"{BASE}/accruals/{accrual}/reverse",
        headers=limited_headers,
        json={"amount": 500, "reason": "Проверка права"},
    )
    assert denied.status_code == 403


def test_reversal_refuses_more_than_recognized(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Откатить больше, чем в расходе, нельзя — иначе дебиторка ушла бы в минус."""

    async def seed() -> uuid.UUID:
        async with async_session_factory() as session:
            _, prepayment_id = await _recognized_payment(
                session, name="Откат Перебор", inn="6155000605", amount="1500.00"
            )
            return prepayment_id

    prepayment_id = asyncio.run(seed())
    headers = _admin(async_session_factory)
    client.post(
        f"{BASE}/prepayments/{prepayment_id}/recognize",
        headers=headers,
        json={"service_period_start": "2026-06-01", "service_period_end": "2026-06-30"},
    )

    async def accrual_id() -> uuid.UUID:
        async with async_session_factory() as session:
            row = await session.scalar(
                select(SupplierExpenseAccrual)
                .join(SupplierInvoice, SupplierInvoice.id == SupplierExpenseAccrual.invoice_id)
                .where(SupplierInvoice.external_id.like(f"self:{prepayment_id}:%"))
            )
            return row.id

    accrual = asyncio.run(accrual_id())
    response = client.post(
        f"{BASE}/accruals/{accrual}/reverse",
        headers=headers,
        json={"amount": 9000, "reason": "Слишком много"},
    )
    assert response.status_code == 409
    assert "больше, чем признано" in response.json()["detail"]


def test_full_reversal_survives_nightly_job(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Полностью откаченный месяц ночная джоба НЕ признаёт заново.

    Откат освобождал ключ идемпотентности ``self:{платёж}:{месяц}`` — «чтобы месяц можно было
    признать заново». Следствие: в 00:04 МСК ``accrue_due_months`` не находила месяц закрытым
    и заводила самоакт снова. Решение владельца жило до ближайшей ночи, расход возвращался
    сам собой и без следа в журнале. Теперь занятый ключ и есть отметка «откачено вручную».
    """

    async def seed() -> uuid.UUID:
        async with async_session_factory() as session:
            _, prepayment_id = await _recognized_payment(
                session, name="Откат Полный Ночь", inn="6155000610", amount="3000.00"
            )
            return prepayment_id

    prepayment_id = asyncio.run(seed())
    headers = _admin(async_session_factory)
    recognized = client.post(
        f"{BASE}/prepayments/{prepayment_id}/recognize",
        headers=headers,
        json={"service_period_start": "2026-06-01", "service_period_end": "2026-06-30"},
    )
    assert recognized.status_code == 200, recognized.text

    async def accrual_id() -> uuid.UUID:
        async with async_session_factory() as session:
            row = await session.scalar(
                select(SupplierExpenseAccrual)
                .join(SupplierInvoice, SupplierInvoice.id == SupplierExpenseAccrual.invoice_id)
                .where(SupplierInvoice.external_id.like(f"self:{prepayment_id}:%"))
            )
            return row.id

    accrual = asyncio.run(accrual_id())
    response = client.post(
        f"{BASE}/accruals/{accrual}/reverse",
        headers=headers,
        json={"amount": 3000, "reason": "Лицензию не подключали — расхода не было"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["fully_cancelled"] is True

    async def nightly_and_check() -> None:
        from app.services.subscription_accruals import accrue_due_months

        async with async_session_factory() as session:
            # Ровно то, что делает ночная джоба на следующий день.
            await accrue_due_months(session, as_of=date(2026, 7, 2), commit=True)

            live = await session.scalars(
                select(SupplierInvoice).where(
                    SupplierInvoice.external_id.like(f"self:{prepayment_id}:%"),
                    SupplierInvoice.payment_status != "void",
                )
            )
            assert live.all() == [], "джоба воскресила откаченный месяц"
            recognized_rows = await session.scalars(
                select(SupplierExpenseAccrual).where(
                    SupplierExpenseAccrual.counterparty_id
                    == (await session.get(SupplierPrepayment, prepayment_id)).counterparty_id,
                    SupplierExpenseAccrual.status != "cancelled",
                )
            )
            assert recognized_rows.all() == [], "расход вернулся в P&L после отката"

    asyncio.run(nightly_and_check())


def test_manual_recognition_after_reversal_explains_itself(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Повторное ручное признание откаченного месяца отказывает ВНЯТНО, а не молча.

    Ключ месяца занят, поэтому ``ensure_month_accrual`` вернул бы None по каждому месяцу и
    кнопка «признать» отработала бы вхолостую: ни строки, ни объяснения.
    """

    async def seed() -> uuid.UUID:
        async with async_session_factory() as session:
            _, prepayment_id = await _recognized_payment(
                session, name="Откат Повторный", inn="6155000611", amount="3000.00"
            )
            return prepayment_id

    prepayment_id = asyncio.run(seed())
    headers = _admin(async_session_factory)
    period = {"service_period_start": "2026-06-01", "service_period_end": "2026-06-30"}
    assert (
        client.post(
            f"{BASE}/prepayments/{prepayment_id}/recognize", headers=headers, json=period
        ).status_code
        == 200
    )

    async def accrual_id() -> uuid.UUID:
        async with async_session_factory() as session:
            row = await session.scalar(
                select(SupplierExpenseAccrual)
                .join(SupplierInvoice, SupplierInvoice.id == SupplierExpenseAccrual.invoice_id)
                .where(SupplierInvoice.external_id.like(f"self:{prepayment_id}:%"))
            )
            return row.id

    accrual = asyncio.run(accrual_id())
    assert (
        client.post(
            f"{BASE}/accruals/{accrual}/reverse",
            headers=headers,
            json={"amount": 3000, "reason": "Расхода не было"},
        ).status_code
        == 200
    )

    again = client.post(
        f"{BASE}/prepayments/{prepayment_id}/recognize", headers=headers, json=period
    )
    assert again.status_code == 409, again.text
    assert "откачено вручную" in again.json()["detail"]
    assert "06.2026" in again.json()["detail"]

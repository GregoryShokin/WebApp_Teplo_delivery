"""АУДИТ-4, третья волна: заявки по слою ВЫДАЧИ чисел (плитки и реестры дашборда).

В отличие от первых двух волн, эти дефекты живут в моём коде (ручки /balances, /payments),
а не в канон-слое.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from cp_helpers import admin_headers, make_counterparty, make_invoice, make_wallet
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BASE = "/api/v1/accounting/suppliers"


@pytest.mark.skip(
    reason="Открытый МЕТОДОЛОГИЧЕСКИЙ вопрос владельцу: бартерный заём — реальное обязательство, "
    "но ни один контур гашения его не трогает (все фильтруют barter_role IS NULL), т.е. такая КЗ "
    "в плитке может только расти. Либо гасить бартер, либо не показывать в КЗ — решает владелец."
)
async def test_barter_invoice_not_counted_as_payable(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Заявка: бартерные накладные попадают в плитку КЗ, хотя ни один контур гашения их не
    трогает (``_settle_open_kz_from_transaction`` и
    ``_settle_counterparty_closing_from_prepayments``
    фильтруют ``barter_role IS NULL``, а запрос плитки — нет). Такая КЗ неснижаема."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Бартер-КЗ", inn="6155992001")
        await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="5000.00",
            doc_kind="closing",
            barter_role="loan",
            number="БАРТЕР-1",
            invoice_date=date(2026, 7, 1),
        )
        await session.commit()
        cp_id = str(cp.id)

    headers = await admin_headers(async_session_factory)
    response = client.get(f"{BASE}/balances", headers=headers)
    assert response.status_code == 200
    row = next((i for i in response.json()["items"] if i["counterparty_id"] == cp_id), None)
    payable = Decimal(str(row["payable"])) if row else Decimal("0.00")
    assert payable == Decimal("0.00"), (
        f"Бартерный заём 5000 показан кредиторкой ({payable}) — у бартера свой нетто-контур, "
        "и ни одна дверь гашения эту КЗ снизить не может"
    )


async def test_paid_bill_shows_single_payment_row(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Заявка: ДЗ по оплаченному счёту (``prepaid_bill``) имеет ``cashflow_transaction_id=None``
    ПО ЗАМЫСЛУ (денег не двигает) — и ровно поэтому попадает в реестр /payments как строка
    «начальный остаток». Один платёж по счёту = две строки и задвоенный итог периода."""
    from cp_helpers import make_expense_article

    from app.services.counterparty_payments import pay_invoice_from_wallet

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Реестр-счёт", inn="6155992002")
        bill = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="800.00",
            doc_kind="bill",
            number="СЧ-реестр",
            invoice_date=date(2026, 7, 1),
        )
        wallet = await make_wallet(session, name="Касса-реестр", wallet_type="cash_safe")
        article = await make_expense_article(session)
        await session.commit()
        await pay_invoice_from_wallet(
            session,
            invoice_id=bill.id,
            wallet_id=wallet.id,
            amount=Decimal("800.00"),
            operation_date=date(2026, 7, 2),
            article_id=article.id,
        )
        cp_id = str(cp.id)

    headers = await admin_headers(async_session_factory)
    response = client.get(f"{BASE}/payments", params={"counterparty_id": cp_id}, headers=headers)
    assert response.status_code == 200
    items = response.json()["items"]
    kinds = [i["row_kind"] for i in items]
    total = sum(Decimal(str(i["amount"])) for i in items)
    assert total == Decimal("800.00"), (
        f"Реестр платежей показывает {total} на один платёж 800 — строки: {kinds}"
    )

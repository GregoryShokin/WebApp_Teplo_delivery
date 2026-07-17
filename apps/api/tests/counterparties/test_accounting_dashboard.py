"""Дашборд взаиморасчётов: остатки по контрагентам, реестр платежей, реестр УПД.

``/accounting/suppliers/{balances,payments,documents}``: дебиторка = открытые предоплаты,
кредиторка = неоплаченный остаток payable-накладных; реестр платежей связывает исходящую
проводку с гашением (прямые аллокации + созданная платежом предоплата и её УПД), реестр
УПД показывает, чем оплачен документ. Бартерные receivable-накладные и void — вне контура.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

from cp_helpers import admin_headers, make_counterparty, make_invoice, make_wallet
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import CashflowTransaction, InvoicePaymentAllocation, SupplierPrepayment

BASE = "/api/v1/accounting/suppliers"


def _admin(factory) -> dict[str, str]:
    return asyncio.run(admin_headers(factory))


async def _make_tx(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
    counterparty_id: uuid.UUID,
    amount: str,
    operation_date: date,
) -> CashflowTransaction:
    tx = CashflowTransaction(
        wallet_id=wallet_id,
        direction="out",
        amount=Decimal(amount),
        operation_date=operation_date,
        counterparty_id=counterparty_id,
        source_kind="bank_feed",
    )
    session.add(tx)
    await session.flush()
    return tx


async def _make_prepayment(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    amount: str,
    settled: str = "0.00",
    status: str = "open",
    kind: str = "goods",
    cashflow_transaction_id: uuid.UUID | None = None,
    note: str | None = None,
) -> SupplierPrepayment:
    prepayment = SupplierPrepayment(
        counterparty_id=counterparty_id,
        kind=kind,
        amount=Decimal(amount),
        amount_settled=Decimal(settled),
        status=status,
        cashflow_transaction_id=cashflow_transaction_id,
        note=note,
    )
    session.add(prepayment)
    await session.flush()
    return prepayment


def test_balances_aggregate_prepayments_and_unpaid_invoices(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """ДЗ = открытый остаток предоплат, КЗ = неоплаченный остаток payable-накладных;
    оплаченные, void и бартерные receivable в остатки не попадают."""

    async def seed() -> uuid.UUID:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Дашборд Баланс А", inn="6155000101")
            await _make_prepayment(
                session,
                counterparty_id=cp.id,
                amount="1000.00",
                settled="300.00",
                status="partially_settled",
            )
            invoice = await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="500.00",
                invoice_date=date(2026, 6, 10),
            )
            wallet = await make_wallet(session, name="Дашборд Касса А")
            tx = await _make_tx(
                session,
                wallet_id=wallet.id,
                counterparty_id=cp.id,
                amount="200.00",
                operation_date=date(2026, 6, 11),
            )
            session.add(
                InvoicePaymentAllocation(
                    invoice_id=invoice.id,
                    source_kind="cash",
                    cashflow_transaction_id=tx.id,
                    amount=Decimal("200.00"),
                )
            )
            invoice.payment_status = "partially_paid"

            paid_only = await make_counterparty(session, name="Дашборд Баланс Б", inn="6155000102")
            await make_invoice(
                session, counterparty_id=paid_only.id, amount="700.00", payment_status="paid"
            )
            barter = await make_counterparty(session, name="Дашборд Баланс В", inn="6155000103")
            await make_invoice(
                session,
                counterparty_id=barter.id,
                amount="900.00",
                direction="receivable",
                barter_role="loan",
            )
            await session.commit()
            return cp.id

    cp_id = asyncio.run(seed())
    response = client.get(f"{BASE}/balances", headers=_admin(async_session_factory))
    assert response.status_code == 200
    payload = response.json()
    by_id = {item["counterparty_id"]: item for item in payload["items"]}

    item = by_id[str(cp_id)]
    assert item["receivable"] == 700.0
    assert item["payable"] == 300.0
    assert item["net"] == 400.0
    assert item["open_prepayments"] == 1
    assert item["unpaid_invoices"] == 1

    names = {item["name"] for item in payload["items"]}
    assert "Дашборд Баланс Б" not in names  # только оплаченные накладные — не долг
    assert "Дашборд Баланс В" not in names  # бартерная receivable — свой контур
    assert payload["receivable_total"] >= 700.0
    assert payload["payable_total"] >= 300.0


def test_payments_register_links_settlements(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Строка платежа несёт прямые гашения накладных, созданную предоплату с её УПД
    и свободный остаток; входящий остаток — отдельная строка без движения денег."""

    async def seed() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Дашборд Платежи", inn="6155000104")
            wallet = await make_wallet(session, name="Дашборд Банк")

            invoice_direct = await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="500.00",
                number="ПД-1",
                payment_status="paid",
                invoice_date=date(2026, 6, 4),
            )
            tx_direct = await _make_tx(
                session,
                wallet_id=wallet.id,
                counterparty_id=cp.id,
                amount="500.00",
                operation_date=date(2026, 6, 5),
            )
            session.add(
                InvoicePaymentAllocation(
                    invoice_id=invoice_direct.id,
                    source_kind="cash",
                    cashflow_transaction_id=tx_direct.id,
                    amount=Decimal("500.00"),
                )
            )

            tx_prepay = await _make_tx(
                session,
                wallet_id=wallet.id,
                counterparty_id=cp.id,
                amount="1000.00",
                operation_date=date(2026, 6, 7),
            )
            prepayment = await _make_prepayment(
                session,
                counterparty_id=cp.id,
                amount="1000.00",
                settled="400.00",
                status="partially_settled",
                kind="subscription",
                cashflow_transaction_id=tx_prepay.id,
            )
            invoice_upd = await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="400.00",
                number="УПД-1",
                payment_status="paid",
                invoice_date=date(2026, 6, 30),
            )
            session.add(
                InvoicePaymentAllocation(
                    invoice_id=invoice_upd.id,
                    source_kind="prepayment",
                    prepayment_id=prepayment.id,
                    amount=Decimal("400.00"),
                )
            )

            await _make_prepayment(
                session,
                counterparty_id=cp.id,
                amount="200.00",
                note="Входящий остаток на 01.06",
            )
            await session.commit()
            return cp.id, tx_direct.id, tx_prepay.id, invoice_upd.id

    cp_id, tx_direct_id, tx_prepay_id, _ = asyncio.run(seed())
    response = client.get(
        f"{BASE}/payments",
        params={"counterparty_id": str(cp_id), "date_from": "2026-06-01"},
        headers=_admin(async_session_factory),
    )
    assert response.status_code == 200
    rows = {row["id"]: row for row in response.json()["items"]}

    direct = rows[str(tx_direct_id)]
    assert direct["row_kind"] == "transaction"
    assert [ref["number"] for ref in direct["settled_invoices"]] == ["ПД-1"]
    assert direct["settled_invoices"][0]["amount"] == 500.0
    assert direct["prepayment"] is None
    assert direct["unassigned_amount"] == 0.0

    prepay_row = rows[str(tx_prepay_id)]
    assert prepay_row["prepayment"] is not None
    assert prepay_row["prepayment"]["amount_settled"] == 400.0
    assert [ref["number"] for ref in prepay_row["prepayment"]["settled_invoices"]] == ["УПД-1"]
    assert prepay_row["unassigned_amount"] == 0.0

    opening = [row for row in rows.values() if row["row_kind"] == "opening_prepayment"]
    assert len(opening) == 1
    assert opening[0]["amount"] == 200.0
    assert opening[0]["purpose"] == "Входящий остаток на 01.06"


def test_documents_register_shows_payment_breakdown(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """УПД несёт разбивку оплаты по источникам и честный остаток; void — скрыт."""

    async def seed() -> tuple[uuid.UUID, uuid.UUID]:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Дашборд УПД", inn="6155000105")
            wallet = await make_wallet(session, name="Дашборд УПД Касса")
            invoice = await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="600.00",
                number="УПД-2",
                payment_status="partially_paid",
                invoice_date=date(2026, 6, 15),
            )
            prepayment = await _make_prepayment(
                session,
                counterparty_id=cp.id,
                amount="300.00",
                settled="300.00",
                status="settled",
            )
            tx = await _make_tx(
                session,
                wallet_id=wallet.id,
                counterparty_id=cp.id,
                amount="200.00",
                operation_date=date(2026, 6, 16),
            )
            session.add(
                InvoicePaymentAllocation(
                    invoice_id=invoice.id,
                    source_kind="prepayment",
                    prepayment_id=prepayment.id,
                    amount=Decimal("300.00"),
                )
            )
            session.add(
                InvoicePaymentAllocation(
                    invoice_id=invoice.id,
                    source_kind="cash",
                    cashflow_transaction_id=tx.id,
                    amount=Decimal("200.00"),
                )
            )
            await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="123.00",
                number="ВОЙД-1",
                payment_status="void",
                invoice_date=date(2026, 6, 20),
            )
            await session.commit()
            return cp.id, invoice.id

    cp_id, invoice_id = asyncio.run(seed())
    response = client.get(
        f"{BASE}/documents",
        params={"counterparty_id": str(cp_id), "date_from": "2026-06-01"},
        headers=_admin(async_session_factory),
    )
    assert response.status_code == 200
    payload = response.json()
    numbers = [row["number"] for row in payload["items"]]
    assert "ВОЙД-1" not in numbers

    row = next(item for item in payload["items"] if item["invoice_id"] == str(invoice_id))
    assert row["amount"] == 600.0
    assert row["remainder"] == 100.0
    kinds = sorted((alloc["source_kind"], alloc["amount"]) for alloc in row["allocations"])
    assert kinds == [("cash", 200.0), ("prepayment", 300.0)]
    prepay_alloc = next(a for a in row["allocations"] if a["source_kind"] == "prepayment")
    assert prepay_alloc["prepayment_kind"] == "goods"
    assert payload["unpaid_total"] >= 100.0

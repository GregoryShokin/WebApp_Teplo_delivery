"""Phase 4 split payment: pay one invoice from N sources (bank + cash) in one transaction,
with the «персонал» part booked to its own DDS article. Exercised on ``teplo_test``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from cp_helpers import (
    make_bank_operation,
    make_counterparty,
    make_expense_article,
    make_invoice,
    make_wallet,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import CashflowTransaction, InvoicePaymentAllocation, SupplierInvoice
from app.services.warehouse_payments import (
    BankPart,
    CashPart,
    WarehousePaymentError,
    build_staff_split_cash_parts,
    pay_invoice_split,
    resolve_staff_article_id,
)

OP_DATE = date(2026, 6, 10)


async def _articles(session: AsyncSession) -> None:
    await make_expense_article(session, code="payment_to_supplier", name="Оплата поставщикам")
    await make_expense_article(
        session, code="supplier_staff_payment", name="Оплата поставщику (персонал)"
    )


async def _alloc_count(session: AsyncSession, invoice_id, kind: str) -> int:
    return await session.scalar(
        select(func.count())
        .select_from(InvoicePaymentAllocation)
        .where(
            InvoicePaymentAllocation.invoice_id == invoice_id,
            InvoicePaymentAllocation.source_kind == kind,
        )
    )


async def _cash_txns(session: AsyncSession, invoice_id) -> list[CashflowTransaction]:
    return list(
        (
            await session.scalars(
                select(CashflowTransaction).where(CashflowTransaction.source_id == invoice_id)
            )
        ).all()
    )


async def test_pay_split_bank_plus_cash_pays_in_full(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _articles(session)
        cp = await make_counterparty(session, name="Поставщик")
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="10000.00")
        op = await make_bank_operation(session, amount="6000.00", operation_date=OP_DATE)
        wallet = await make_wallet(session, name="ТК Черникова", wallet_type="store_cash")
        await session.commit()

        result = await pay_invoice_split(
            session,
            invoice_id=invoice.id,
            bank_parts=[BankPart(bank_operation_id=op.id)],
            cash_parts=[
                CashPart(wallet_id=wallet.id, amount=Decimal("4000.00"), operation_date=OP_DATE)
            ],
        )
        assert result.payment_status == "paid"
        assert await _alloc_count(session, invoice.id, "bank") == 1
        assert await _alloc_count(session, invoice.id, "cash") == 1
        # Only the cash part creates a DDS expense; the bank op is its own statement line.
        txns = await _cash_txns(session, invoice.id)
        assert len(txns) == 1 and txns[0].amount == Decimal("4000.00")


async def test_pay_split_partial_leaves_remaining(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _articles(session)
        cp = await make_counterparty(session, name="P")
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="10000.00")
        op = await make_bank_operation(session, amount="6000.00", operation_date=OP_DATE)
        await session.commit()

        result = await pay_invoice_split(
            session, invoice_id=invoice.id, bank_parts=[BankPart(bank_operation_id=op.id)]
        )
        assert result.payment_status == "partially_paid"


async def test_pay_split_rejects_occupied_operation_without_writing(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _articles(session)
        cp = await make_counterparty(session, name="P")
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="6000.00")
        other = await make_invoice(session, counterparty_id=cp.id, amount="6000.00", number="X2")
        op = await make_bank_operation(session, amount="6000.00", operation_date=OP_DATE)
        session.add(
            InvoicePaymentAllocation(
                invoice_id=other.id, source_kind="bank",
                bank_operation_id=op.id, amount=Decimal("6000.00"),
            )
        )
        await session.commit()
        invoice_id = invoice.id  # keep before the rollback expires the instance

        with pytest.raises(WarehousePaymentError):
            await pay_invoice_split(
                session, invoice_id=invoice_id, bank_parts=[BankPart(bank_operation_id=op.id)]
            )
        # The target invoice stayed untouched — no partial write.
        assert await _alloc_count(session, invoice_id, "bank") == 0
        refreshed = await session.get(SupplierInvoice, invoice_id)
        assert refreshed.payment_status == "unpaid"


async def test_pay_split_rejects_overpayment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _articles(session)
        cp = await make_counterparty(session, name="P")
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="10000.00")
        wallet = await make_wallet(session, name="Касса")
        await session.commit()
        invoice_id = invoice.id  # keep before the rollback expires the instance

        with pytest.raises(WarehousePaymentError):
            await pay_invoice_split(
                session,
                invoice_id=invoice_id,
                cash_parts=[
                    CashPart(
                        wallet_id=wallet.id, amount=Decimal("12000.00"), operation_date=OP_DATE
                    )
                ],
            )
        # No orphan cash transaction was created.
        assert await _cash_txns(session, invoice_id) == []


async def test_pay_split_rejects_receivable_and_barter(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Guard: only a plain payable can be paid with money — not AR (receivable) or barter."""
    async with async_session_factory() as session:
        await _articles(session)
        cp = await make_counterparty(session, name="P")
        ar = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", direction="receivable"
        )
        loan = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", barter_role="loan", number="L1"
        )
        wallet = await make_wallet(session, name="Касса")
        await session.commit()
        cash = [CashPart(wallet_id=wallet.id, amount=Decimal("100.00"), operation_date=OP_DATE)]

        for invoice in (ar, loan):
            with pytest.raises(WarehousePaymentError):
                await pay_invoice_split(session, invoice_id=invoice.id, cash_parts=cash)


async def test_pay_split_bank_part_cannot_exceed_operation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A bank part with an explicit amount above the operation's own amount is rejected —
    one operation can't allocate more than the money that actually moved."""
    async with async_session_factory() as session:
        await _articles(session)
        cp = await make_counterparty(session, name="P")
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="10000.00")
        op = await make_bank_operation(session, amount="1000.00", operation_date=OP_DATE)
        await session.commit()
        invoice_id = invoice.id

        with pytest.raises(WarehousePaymentError):
            await pay_invoice_split(
                session,
                invoice_id=invoice_id,
                bank_parts=[BankPart(bank_operation_id=op.id, amount=Decimal("5000.00"))],
            )
        assert await _alloc_count(session, invoice_id, "bank") == 0


async def test_staff_split_books_two_articles(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _articles(session)
        staff_article_id = await resolve_staff_article_id(session)
        cp = await make_counterparty(session, name="P")
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="10000.00", staff_amount="2000.00"
        )
        wallet = await make_wallet(session, name="ТК Черникова", wallet_type="store_cash")
        await session.commit()

        parts = await build_staff_split_cash_parts(
            session, invoice, wallet_id=wallet.id, operation_date=OP_DATE
        )
        assert {p.amount for p in parts} == {Decimal("8000.00"), Decimal("2000.00")}

        result = await pay_invoice_split(session, invoice_id=invoice.id, cash_parts=parts)
        assert result.payment_status == "paid"
        txns = await _cash_txns(session, invoice.id)
        assert len(txns) == 2
        by_article = {t.article_id: t.amount for t in txns}
        assert by_article[staff_article_id] == Decimal("2000.00")

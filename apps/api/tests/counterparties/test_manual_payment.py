"""Manual payment of a payable invoice from a DDS wallet.

Creates a real DDS expense (cashflow_transaction) on the chosen wallet and allocates
it to the invoice — supports partial/split payments; the unpaid remainder can still be
sent to the bank as a draft.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from cp_helpers import (
    make_counterparty,
    make_expense_article,
    make_invoice,
    make_wallet,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import CashflowTransaction
from app.services.counterparty_payments import (
    CounterpartyPaymentError,
    create_payment_draft_for_invoices,
    pay_invoice_from_wallet,
)

PAY_DATE = date(2026, 6, 11)
VERIFIED_REQUISITES = {
    "bankAcnt": "40702810400000012345",
    "bankBik": "044525225",
    "recipientCorrAccountNumber": "30101810400000000225",
}


async def test_pay_full_marks_paid_and_creates_dds_expense(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Местный закуп", inn="7701234567", relationship="informal"
        )
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", number="LC-1"
        )
        wallet = await make_wallet(session, name="ТК Черникова", wallet_type="store_cash")
        await make_expense_article(session)  # payment_to_supplier
        await session.commit()

        result = await pay_invoice_from_wallet(
            session,
            invoice_id=invoice.id,
            wallet_id=wallet.id,
            amount=Decimal("1000.00"),
            operation_date=PAY_DATE,
            actor_user_id=None,
        )

        assert result.payment_status == "paid"
        txn = await session.scalar(select(CashflowTransaction))
        assert txn is not None
        assert txn.direction == "out"
        assert txn.amount == Decimal("1000.00")
        assert txn.wallet_id == wallet.id
        assert txn.counterparty_id == cp.id
        assert txn.source_kind == "counterparty_payment"
        assert txn.source_id == invoice.id
        assert txn.article_id is not None  # defaulted to «Оплата поставщикам»
        assert txn.quality_status == "final"


async def test_pay_partial_then_remainder_to_bank(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Half cash now, the unpaid remainder still draftable to the bank."""
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session,
            name="ООО Поставщик",
            inn="7701234567",
            requisites=VERIFIED_REQUISITES,
            requisites_verified=True,
        )
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="1000.00")
        wallet = await make_wallet(session)
        await make_expense_article(session)
        await session.commit()

        await pay_invoice_from_wallet(
            session,
            invoice_id=invoice.id,
            wallet_id=wallet.id,
            amount=Decimal("400.00"),
            operation_date=PAY_DATE,
            actor_user_id=None,
        )
        await session.refresh(invoice)
        assert invoice.payment_status == "partially_paid"

        draft = await create_payment_draft_for_invoices(
            session, invoice_ids=[invoice.id], actor_user_id=None
        )
        assert draft.amount == Decimal("600.00")  # only the unpaid remainder


async def test_pay_split_two_cash_payments_closes_invoice(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="X", inn="7702222222", relationship="informal")
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="1000.00")
        cash = await make_wallet(session, name="Сейф", wallet_type="cash_safe")
        card = await make_wallet(session, name="Тинькофф", wallet_type="bank")
        await make_expense_article(session)
        await session.commit()

        await pay_invoice_from_wallet(
            session,
            invoice_id=invoice.id,
            wallet_id=cash.id,
            amount=Decimal("400.00"),
            operation_date=PAY_DATE,
            actor_user_id=None,
        )
        await pay_invoice_from_wallet(
            session,
            invoice_id=invoice.id,
            wallet_id=card.id,
            amount=Decimal("600.00"),
            operation_date=PAY_DATE,
            actor_user_id=None,
        )

        await session.refresh(invoice)
        assert invoice.payment_status == "paid"
        txn_count = await session.scalar(select(func.count()).select_from(CashflowTransaction))
        assert txn_count == 2  # one DDS expense per payment


async def test_pay_with_explicit_article(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="X", inn="7703333333", relationship="informal")
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="500.00")
        wallet = await make_wallet(session)
        article = await make_expense_article(session, code="local_purchase", name="Местный закуп")
        await session.commit()

        await pay_invoice_from_wallet(
            session,
            invoice_id=invoice.id,
            wallet_id=wallet.id,
            amount=Decimal("500.00"),
            operation_date=PAY_DATE,
            article_id=article.id,
            actor_user_id=None,
        )

        txn = await session.scalar(select(CashflowTransaction))
        assert txn.article_id == article.id


async def test_pay_rejects_over_remaining(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="X", inn="7704444444", relationship="informal")
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="1000.00")
        wallet = await make_wallet(session)
        await make_expense_article(session)
        await session.commit()

        with pytest.raises(CounterpartyPaymentError, match="остатка"):
            await pay_invoice_from_wallet(
                session,
                invoice_id=invoice.id,
                wallet_id=wallet.id,
                amount=Decimal("1500.00"),
                operation_date=PAY_DATE,
                actor_user_id=None,
            )


async def test_pay_rejects_receivable_invoice(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Барт", inn="7705555555", relationship="barter")
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", direction="receivable"
        )
        wallet = await make_wallet(session)
        await make_expense_article(session)
        await session.commit()

        with pytest.raises(CounterpartyPaymentError, match="Доходную"):
            await pay_invoice_from_wallet(
                session,
                invoice_id=invoice.id,
                wallet_id=wallet.id,
                amount=Decimal("100.00"),
                operation_date=PAY_DATE,
                actor_user_id=None,
            )


async def test_pay_rejects_void_invoice(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="X", inn="7706666666", relationship="informal")
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", payment_status="void"
        )
        wallet = await make_wallet(session)
        await make_expense_article(session)
        await session.commit()

        with pytest.raises(CounterpartyPaymentError, match="аннулирована"):
            await pay_invoice_from_wallet(
                session,
                invoice_id=invoice.id,
                wallet_id=wallet.id,
                amount=Decimal("100.00"),
                operation_date=PAY_DATE,
                actor_user_id=None,
            )


async def test_pay_rejects_unknown_wallet(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="X", inn="7707777777", relationship="informal")
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="1000.00")
        await make_expense_article(session)
        await session.commit()

        with pytest.raises(CounterpartyPaymentError, match="Счёт не найден"):
            await pay_invoice_from_wallet(
                session,
                invoice_id=invoice.id,
                wallet_id=uuid.uuid4(),
                amount=Decimal("100.00"),
                operation_date=PAY_DATE,
                actor_user_id=None,
            )

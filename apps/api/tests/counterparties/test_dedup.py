"""Dedup: a manual payment from a bank wallet must not double-book when the same
operation later arrives in the T-Bank statement.

``pay_invoice_from_wallet`` pre-books a DDS expense (source_kind
``counterparty_payment``). When the bank feed imports that operation, the classifier
must LINK it to the pre-booked entry instead of creating a second ``bank_operation``
expense — otherwise the ДДС double-counts the payment. Cash/card entries on other
wallets are never touched.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from cp_helpers import (
    make_account,
    make_bank_operation,
    make_counterparty,
    make_expense_article,
    make_invoice,
    make_wallet,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import CashflowTransaction, ClassificationRule
from app.services.banking.classifier import apply_operation_action, run_classification_rules
from app.services.counterparty_payments import pay_invoice_from_wallet

PAY_DATE = date(2026, 6, 11)
SUPPLIER_INN = "7701234567"


async def _cashflow_count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(CashflowTransaction))


async def test_bank_feed_links_prebooked_payment_no_double_expense(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        account = await make_account(session)
        wallet = await make_wallet(
            session, name="T-Bank", wallet_type="bank", account_id=account.id
        )
        await make_expense_article(session)
        cp = await make_counterparty(session, name="ООО Поставщик", inn=SUPPLIER_INN)
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="5000.00", number="INV-1"
        )
        await session.commit()

        invoice = await pay_invoice_from_wallet(
            session,
            invoice_id=invoice.id,
            wallet_id=wallet.id,
            amount=Decimal("5000.00"),
            operation_date=PAY_DATE,
            actor_user_id=None,
        )
        assert invoice.payment_status == "paid"
        prebooked = await session.scalar(select(CashflowTransaction))
        assert prebooked.source_kind == "counterparty_payment"

        # The same payment shows up in the statement a day later.
        op = await make_bank_operation(
            session,
            amount="5000.00",
            direction="out",
            inn=SUPPLIER_INN,
            account_id=account.id,
            operation_date=date(2026, 6, 12),
        )
        result = await run_classification_rules(session, [op])

        assert result.classified == 1
        assert op.cashflow_transaction_id == prebooked.id
        assert op.classification_status == "classified"
        # Linked, not duplicated — still exactly one expense.
        assert await _cashflow_count(session) == 1


async def test_cash_payment_not_touched_by_bank_feed(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A payment booked on a cash wallet is on a different wallet than the bank op resolves
    to — it must never be claimed by the statement reconcile."""
    async with async_session_factory() as session:
        account = await make_account(session)
        await make_wallet(session, name="T-Bank", wallet_type="bank", account_id=account.id)
        cash = await make_wallet(session, name="Сейф", wallet_type="cash_safe")
        await make_expense_article(session)
        cp = await make_counterparty(
            session, name="Местный закуп", inn=SUPPLIER_INN, relationship="informal"
        )
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="5000.00")
        await session.commit()

        await pay_invoice_from_wallet(
            session,
            invoice_id=invoice.id,
            wallet_id=cash.id,
            amount=Decimal("5000.00"),
            operation_date=PAY_DATE,
            actor_user_id=None,
        )
        op = await make_bank_operation(
            session,
            amount="5000.00",
            direction="out",
            inn=SUPPLIER_INN,
            account_id=account.id,
            operation_date=PAY_DATE,
        )
        result = await run_classification_rules(session, [op])

        assert result.classified == 0
        assert result.needs_review == 1
        assert op.cashflow_transaction_id is None
        assert await _cashflow_count(session) == 1  # the cash entry, untouched


async def test_amount_mismatch_not_linked(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        account = await make_account(session)
        wallet = await make_wallet(
            session, name="T-Bank", wallet_type="bank", account_id=account.id
        )
        await make_expense_article(session)
        cp = await make_counterparty(session, name="ООО Поставщик", inn=SUPPLIER_INN)
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="5000.00")
        await session.commit()

        await pay_invoice_from_wallet(
            session,
            invoice_id=invoice.id,
            wallet_id=wallet.id,
            amount=Decimal("5000.00"),
            operation_date=PAY_DATE,
            actor_user_id=None,
        )
        op = await make_bank_operation(
            session,
            amount="4999.00",  # off by one rouble — not the same payment
            direction="out",
            inn=SUPPLIER_INN,
            account_id=account.id,
            operation_date=PAY_DATE,
        )
        result = await run_classification_rules(session, [op])

        assert result.classified == 0
        assert op.cashflow_transaction_id is None
        assert await _cashflow_count(session) == 1


async def test_inn_mismatch_not_linked(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When the statement carries a different INN than the pre-booked payee, never link."""
    async with async_session_factory() as session:
        account = await make_account(session)
        wallet = await make_wallet(
            session, name="T-Bank", wallet_type="bank", account_id=account.id
        )
        await make_expense_article(session)
        cp = await make_counterparty(session, name="ООО Поставщик", inn=SUPPLIER_INN)
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="5000.00")
        await session.commit()

        await pay_invoice_from_wallet(
            session,
            invoice_id=invoice.id,
            wallet_id=wallet.id,
            amount=Decimal("5000.00"),
            operation_date=PAY_DATE,
            actor_user_id=None,
        )
        op = await make_bank_operation(
            session,
            amount="5000.00",
            direction="out",
            inn="7709999999",  # a different payee, same round amount
            account_id=account.id,
            operation_date=PAY_DATE,
        )
        result = await run_classification_rules(session, [op])

        assert result.classified == 0
        assert op.cashflow_transaction_id is None
        assert await _cashflow_count(session) == 1


async def test_date_outside_window_not_linked(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        account = await make_account(session)
        wallet = await make_wallet(
            session, name="T-Bank", wallet_type="bank", account_id=account.id
        )
        await make_expense_article(session)
        cp = await make_counterparty(session, name="ООО Поставщик", inn=SUPPLIER_INN)
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="5000.00")
        await session.commit()

        await pay_invoice_from_wallet(
            session,
            invoice_id=invoice.id,
            wallet_id=wallet.id,
            amount=Decimal("5000.00"),
            operation_date=PAY_DATE,
            actor_user_id=None,
        )
        op = await make_bank_operation(
            session,
            amount="5000.00",
            direction="out",
            inn=SUPPLIER_INN,
            account_id=account.id,
            operation_date=date(2026, 6, 20),  # +9 days, outside the ±3-day window
        )
        result = await run_classification_rules(session, [op])

        assert result.classified == 0
        assert op.cashflow_transaction_id is None
        assert await _cashflow_count(session) == 1


async def test_two_prebooked_two_ops_link_one_to_one(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two equal-amount payments + two equal-amount ops link 1:1, never both to one entry."""
    async with async_session_factory() as session:
        account = await make_account(session)
        wallet = await make_wallet(
            session, name="T-Bank", wallet_type="bank", account_id=account.id
        )
        await make_expense_article(session)
        cp = await make_counterparty(session, name="ООО Поставщик", inn=None)
        inv_a = await make_invoice(session, counterparty_id=cp.id, amount="5000.00", number="A")
        inv_b = await make_invoice(session, counterparty_id=cp.id, amount="5000.00", number="B")
        await session.commit()

        for inv in (inv_a, inv_b):
            await pay_invoice_from_wallet(
                session,
                invoice_id=inv.id,
                wallet_id=wallet.id,
                amount=Decimal("5000.00"),
                operation_date=PAY_DATE,
                actor_user_id=None,
            )
        # Two statement ops without an INN → FIFO matching.
        op1 = await make_bank_operation(
            session, amount="5000.00", direction="out", account_id=account.id,
            operation_date=PAY_DATE, provider_operation_id="op-1",
        )
        op2 = await make_bank_operation(
            session, amount="5000.00", direction="out", account_id=account.id,
            operation_date=PAY_DATE, provider_operation_id="op-2",
        )
        result = await run_classification_rules(session, [op1, op2])

        assert result.classified == 2
        assert op1.cashflow_transaction_id is not None
        assert op2.cashflow_transaction_id is not None
        assert op1.cashflow_transaction_id != op2.cashflow_transaction_id
        assert await _cashflow_count(session) == 2  # two links, no new entries


async def test_apply_operation_action_links_prebooked(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Manual classification (direct ``apply_operation_action``) also links, not duplicates."""
    async with async_session_factory() as session:
        account = await make_account(session)
        wallet = await make_wallet(
            session, name="T-Bank", wallet_type="bank", account_id=account.id
        )
        article = await make_expense_article(session)
        cp = await make_counterparty(session, name="ООО Поставщик", inn=SUPPLIER_INN)
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="5000.00")
        await session.commit()

        await pay_invoice_from_wallet(
            session,
            invoice_id=invoice.id,
            wallet_id=wallet.id,
            amount=Decimal("5000.00"),
            operation_date=PAY_DATE,
            actor_user_id=None,
        )
        prebooked = await session.scalar(select(CashflowTransaction))

        op = await make_bank_operation(
            session,
            amount="5000.00",
            direction="out",
            inn=SUPPLIER_INN,
            account_id=account.id,
            operation_date=PAY_DATE,
        )
        await apply_operation_action(
            session,
            op,
            action="set_article",
            article_id=article.id,
            counterparty_id=cp.id,
            quality_status="auto",
        )

        assert op.cashflow_transaction_id == prebooked.id
        assert op.classification_status == "classified"
        # The manual entry keeps its own source — it is not converted to bank_operation.
        await session.refresh(prebooked)
        assert prebooked.source_kind == "counterparty_payment"
        assert await _cashflow_count(session) == 1


async def test_rule_books_expense_when_no_prebooked(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Regression: with no pre-booked entry, a matching rule still books the expense."""
    async with async_session_factory() as session:
        account = await make_account(session)
        await make_wallet(session, name="T-Bank", wallet_type="bank", account_id=account.id)
        article = await make_expense_article(session)
        rule = ClassificationRule(
            name="tbank supplier out",
            action="set_article",
            article_id=article.id,
            provider="tbank",
            direction="out",
            amount_min=Decimal("3000.00"),
            amount_max=Decimal("3500.00"),
            priority=10,
            is_active=True,
        )
        session.add(rule)
        await session.flush()

        op = await make_bank_operation(
            session,
            amount="3210.00",
            direction="out",
            account_id=account.id,
            operation_date=PAY_DATE,
        )
        result = await run_classification_rules(session, [op])

        assert result.classified == 1
        booked = await session.scalar(
            select(CashflowTransaction).where(CashflowTransaction.source_kind == "bank_operation")
        )
        assert booked is not None
        assert op.cashflow_transaction_id == booked.id
        assert await _cashflow_count(session) == 1

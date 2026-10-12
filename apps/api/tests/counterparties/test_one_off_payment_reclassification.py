"""Переразбор банковского платежа пересчитывает его разовый самоакт вместе с деньгами."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from cp_helpers import (
    make_account,
    make_bank_operation,
    make_counterparty,
    make_expense_article,
    make_wallet,
)
from sqlalchemy import func, select

from app.models import (
    AccountingPeriodClose,
    CashflowTransaction,
    CounterpartyPayableProfile,
    InvoicePaymentAllocation,
    SupplierExpenseAccrual,
    SupplierExpenseReversal,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services import accounting_periods
from app.services.banking import classifier
from app.services.banking.classifier import (
    OperationSplitLine,
    apply_operation_action,
    apply_operation_split,
)
from app.services.expense_reversal import reverse_expense
from app.services.supplier_prepayments import ensure_prepayment_from_bank_transaction

pytestmark = pytest.mark.usefixtures("migrated_db")
PAID_ON = date(2026, 9, 18)


async def _classified_once(session):
    account = await make_account(session)
    await make_wallet(session, wallet_type="bank", account_id=account.id)
    article = await make_expense_article(session, code=f"once-reclass-{uuid.uuid4().hex}")
    counterparty = await make_counterparty(session, name="Разовый продавец")
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == counterparty.id
        )
    )
    profile.service_billing_mode = "one_off"
    operation = await make_bank_operation(
        session, amount="780.00", operation_date=PAID_ON, account_id=account.id
    )
    await session.commit()
    await apply_operation_action(
        session,
        operation,
        action="set_article",
        article_id=article.id,
        counterparty_id=counterparty.id,
        quality_status="owner_review",
    )
    await session.commit()
    prepayment = await session.scalar(
        select(SupplierPrepayment).where(
            SupplierPrepayment.cashflow_transaction_id == operation.cashflow_transaction_id
        )
    )
    invoice = await session.scalar(
        select(SupplierInvoice).where(SupplierInvoice.external_id == f"once:{prepayment.id}")
    )
    accrual = await session.scalar(
        select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.invoice_id == invoice.id)
    )
    assert prepayment.status == "settled"
    assert accrual.status == "recognized"
    return operation, counterparty, article, prepayment, invoice, accrual


async def _assert_cancelled(session, invoice, accrual):
    await session.refresh(invoice)
    await session.refresh(accrual)
    assert invoice.payment_status == "void"
    assert invoice.amount == Decimal("0.00")
    assert accrual.status == "cancelled"
    assert accrual.amount == Decimal("0.00")
    assert ":reclassified:" in invoice.external_id
    assert (
        await session.scalar(
            select(InvoicePaymentAllocation.id).where(
                InvoicePaymentAllocation.invoice_id == invoice.id
            )
        )
        is None
    )
    reversal = await session.scalar(
        select(SupplierExpenseReversal).where(SupplierExpenseReversal.accrual_id == accrual.id)
    )
    assert reversal.amount == Decimal("780.00")
    assert "Переразбор платежа" in reversal.reason


@pytest.mark.parametrize("door", ["action", "split"])
async def test_reclassifying_one_off_to_cash_counterparty_removes_old_expense(
    async_session_factory, door
):
    async with async_session_factory() as session:
        operation, _cp, article, old_prepayment, invoice, accrual = await _classified_once(session)
        old_transaction_id, old_prepayment_id = (
            operation.cashflow_transaction_id,
            old_prepayment.id,
        )
        cash_counterparty = await make_counterparty(session, name="Расход по оплате")
        await session.commit()
        if door == "action":
            await apply_operation_action(
                session,
                operation,
                action="set_article",
                article_id=article.id,
                counterparty_id=cash_counterparty.id,
                quality_status="owner_review",
            )
        else:
            await apply_operation_split(
                session,
                operation,
                splits=[
                    OperationSplitLine(
                        article.id, Decimal("780.00"), counterparty_id=cash_counterparty.id
                    )
                ],
            )
        await session.commit()
        await _assert_cancelled(session, invoice, accrual)
        current = await session.scalar(
            select(SupplierPrepayment).where(
                SupplierPrepayment.cashflow_transaction_id == operation.cashflow_transaction_id
            )
        )
        assert current.counterparty_id == cash_counterparty.id
        assert current.status == "open"
        assert current.amount == Decimal("780.00")
        assert current.amount_settled == Decimal("0.00")
        if door == "action":
            assert current.id == old_prepayment_id
            assert operation.cashflow_transaction_id == old_transaction_id
        else:
            assert await session.get(CashflowTransaction, old_transaction_id) is None
            # ДЗ прежней разметки не должна остаться сиротой без денежного факта.
            assert (
                await session.scalar(
                    select(SupplierPrepayment.id).where(SupplierPrepayment.id == old_prepayment_id)
                )
                is None
            )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SupplierExpenseAccrual)
                .where(
                    SupplierExpenseAccrual.invoice_id == invoice.id,
                    SupplierExpenseAccrual.status == "recognized",
                )
            )
            == 0
        )


async def test_changed_article_and_amount_rebuild_one_off_in_place(async_session_factory):
    async with async_session_factory() as session:
        operation, cp, _article, prepayment, old_invoice, old_accrual = await _classified_once(
            session
        )
        next_article = await make_expense_article(session, code=f"next-once-{uuid.uuid4().hex}")
        transaction = await session.get(CashflowTransaction, operation.cashflow_transaction_id)
        transaction.article_id = next_article.id
        transaction.amount = Decimal("910.00")
        await ensure_prepayment_from_bank_transaction(session, transaction)
        await session.commit()
        await _assert_cancelled(session, old_invoice, old_accrual)
        current = await session.scalar(
            select(SupplierExpenseAccrual)
            .join(SupplierInvoice, SupplierInvoice.id == SupplierExpenseAccrual.invoice_id)
            .where(SupplierInvoice.external_id == f"once:{prepayment.id}")
        )
        assert current.counterparty_id == cp.id
        assert current.article_id == next_article.id
        assert current.amount == Decimal("910.00")
        assert current.status == "recognized"
        # Повторный проход не создаёт откат/признание на неизменных данных.
        await ensure_prepayment_from_bank_transaction(session, transaction)
        await session.commit()
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SupplierExpenseReversal)
                .where(SupplierExpenseReversal.accrual_id.in_((old_accrual.id, current.id)))
            )
            == 1
        )


@pytest.mark.parametrize("door", ["action", "split"])
async def test_closed_month_blocks_one_off_reclassification(async_session_factory, door):
    async with async_session_factory() as session:
        operation, _cp, article, prepayment, invoice, accrual = await _classified_once(session)
        new_cp = await make_counterparty(session, name="Другой продавец")
        session.add(AccountingPeriodClose(period_month=PAID_ON.replace(day=1)))
        await session.commit()
        original_counterparty, original_transaction = (
            prepayment.counterparty_id,
            (operation.cashflow_transaction_id),
        )
        with pytest.raises(accounting_periods.PeriodClosed, match="09.2026 закрыт"):
            if door == "action":
                await apply_operation_action(
                    session,
                    operation,
                    action="set_article",
                    article_id=article.id,
                    counterparty_id=new_cp.id,
                    quality_status="owner_review",
                )
            else:
                await apply_operation_split(
                    session,
                    operation,
                    splits=[
                        OperationSplitLine(article.id, Decimal("780.00"), counterparty_id=new_cp.id)
                    ],
                )
        await session.refresh(prepayment)
        await session.refresh(accrual)
        await session.refresh(invoice)
        assert prepayment.counterparty_id == original_counterparty
        assert prepayment.cashflow_transaction_id == original_transaction
        assert prepayment.status == "settled"
        assert accrual.status == "recognized"
        assert invoice.payment_status == "paid"


async def test_failure_after_one_off_rollback_restores_the_entire_old_classification(
    async_session_factory, monkeypatch
):
    async with async_session_factory() as session:
        operation, cp, article, prepayment, invoice, accrual = await _classified_once(session)
        invoice_id, accrual_id, prepayment_id = invoice.id, accrual.id, prepayment.id
        operation_id, old_transaction_id = operation.id, operation.cashflow_transaction_id

        async def fail_new_distribution(*_args, **_kwargs):
            raise ValueError("Искусственный отказ нового разбора")

        from app.services import supplier_prepayments

        monkeypatch.setattr(
            supplier_prepayments, "ensure_prepayment_from_bank_transaction", fail_new_distribution
        )
        with pytest.raises(ValueError, match="Искусственный отказ"):
            await classifier.apply_operation_split(
                session,
                operation,
                splits=[OperationSplitLine(article.id, Decimal("780.00"), counterparty_id=cp.id)],
            )
        await session.rollback()
    async with async_session_factory() as session:
        assert await session.get(CashflowTransaction, old_transaction_id) is not None
        old_prepayment = await session.get(SupplierPrepayment, prepayment_id)
        assert old_prepayment.status == "settled"
        assert old_prepayment.cashflow_transaction_id == old_transaction_id
        assert (await session.get(SupplierInvoice, invoice_id)).payment_status == "paid"
        assert (await session.get(SupplierExpenseAccrual, accrual_id)).status == "recognized"
        assert (
            await session.scalar(
                select(SupplierExpenseReversal.id).where(
                    SupplierExpenseReversal.accrual_id == accrual_id
                )
            )
            is None
        )
        from app.models import BankOperation

        assert (await session.get(BankOperation, operation_id)).cashflow_transaction_id == (
            old_transaction_id
        )


async def test_reclassification_does_not_reverse_real_supplier_document(async_session_factory):
    async with async_session_factory() as session:
        operation, cp, article, _prepayment, invoice, accrual = await _classified_once(session)
        # Внешняя первичка управляет расходом сама; разбор ДДС её не отменяет.
        invoice.source = "manual"
        invoice.external_id = None
        await session.commit()
        other = await make_counterparty(session, name="Новый продавец с первичкой")
        await session.commit()
        await apply_operation_action(
            session,
            operation,
            action="set_article",
            article_id=article.id,
            counterparty_id=other.id,
            quality_status="owner_review",
        )
        await session.commit()
        await session.refresh(invoice)
        await session.refresh(accrual)
        assert invoice.counterparty_id == cp.id
        assert invoice.source == "manual"
        assert invoice.amount == Decimal("780.00")
        assert accrual.status == "recognized"
        assert accrual.amount == Decimal("780.00")
        assert (
            await session.scalar(
                select(SupplierExpenseReversal.id).where(
                    SupplierExpenseReversal.accrual_id == accrual.id
                )
            )
            is None
        )


async def test_late_one_off_setting_recognizes_existing_open_receivable(async_session_factory):
    async with async_session_factory() as session:
        operation, cp, article, prepayment, invoice, accrual = await _classified_once(session)
        other = await make_counterparty(session, name="Изначально неразмеченный продавец")
        await session.commit()
        await apply_operation_action(
            session,
            operation,
            action="set_article",
            article_id=article.id,
            counterparty_id=other.id,
            quality_status="owner_review",
        )
        await session.commit()
        profile = await session.scalar(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == other.id
            )
        )
        profile.service_billing_mode = "one_off"
        transaction = await session.get(CashflowTransaction, operation.cashflow_transaction_id)
        await ensure_prepayment_from_bank_transaction(session, transaction)
        await session.commit()
        await session.refresh(prepayment)
        assert prepayment.counterparty_id == other.id
        assert prepayment.status == "settled"
        current = await session.scalar(
            select(SupplierExpenseAccrual).where(
                SupplierExpenseAccrual.counterparty_id == other.id,
                SupplierExpenseAccrual.status == "recognized",
            )
        )
        assert current.amount == Decimal("780.00")


async def test_unchanged_sync_preserves_full_manual_one_off_reversal(async_session_factory):
    async with async_session_factory() as session:
        operation, cp, _article, prepayment, invoice, accrual = await _classified_once(session)
        await reverse_expense(
            session,
            accrual,
            amount=accrual.amount,
            reason="Владелец отменил расход",
            actor_user_id=None,
        )
        transaction = await session.get(CashflowTransaction, operation.cashflow_transaction_id)
        await ensure_prepayment_from_bank_transaction(session, transaction)
        await session.commit()
        await session.refresh(invoice)
        await session.refresh(accrual)
        await session.refresh(prepayment)
        assert invoice.external_id == f"once:{prepayment.id}"
        assert invoice.payment_status == "void"
        assert accrual.status == "cancelled"
        assert prepayment.status == "open"
        assert prepayment.amount_settled == Decimal("0.00")
        assert (
            await session.scalar(
                select(SupplierExpenseAccrual.id).where(
                    SupplierExpenseAccrual.counterparty_id == cp.id,
                    SupplierExpenseAccrual.status == "recognized",
                )
            )
            is None
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SupplierExpenseReversal)
                .where(SupplierExpenseReversal.accrual_id == accrual.id)
            )
            == 1
        )


async def test_changed_one_off_counterparty_replaces_key_after_full_manual_reversal(
    async_session_factory,
):
    async with async_session_factory() as session:
        operation, _cp, article, prepayment, invoice, accrual = await _classified_once(session)
        await reverse_expense(
            session,
            accrual,
            amount=accrual.amount,
            reason="Владелец отменил неверный расход",
            actor_user_id=None,
        )
        other = await make_counterparty(session, name="Другой продавец с признанием по оплате")
        profile = await session.scalar(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == other.id
            )
        )
        profile.service_billing_mode = "one_off"
        await session.commit()
        await apply_operation_action(
            session,
            operation,
            action="set_article",
            article_id=article.id,
            counterparty_id=other.id,
            quality_status="owner_review",
        )
        await session.commit()
        await session.refresh(invoice)
        await session.refresh(accrual)
        await session.refresh(prepayment)
        assert invoice.payment_status == "void"
        assert accrual.status == "cancelled"
        assert ":reclassified:" in invoice.external_id
        assert prepayment.counterparty_id == other.id
        assert prepayment.status == "settled"
        current = await session.scalar(
            select(SupplierExpenseAccrual)
            .join(SupplierInvoice, SupplierInvoice.id == SupplierExpenseAccrual.invoice_id)
            .where(SupplierInvoice.external_id == f"once:{prepayment.id}")
        )
        assert current.counterparty_id == other.id
        assert current.status == "recognized"
        assert current.amount == Decimal("780.00")
        # Ручной откат остаётся в истории; повторного отката нулевого расхода нет.
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SupplierExpenseReversal)
                .where(SupplierExpenseReversal.accrual_id == accrual.id)
            )
            == 1
        )

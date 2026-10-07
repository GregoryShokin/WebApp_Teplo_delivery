"""Возвраты займа и поставщику расходуют разные долги и сходятся в балансе/сверке."""

from datetime import date
from decimal import Decimal

import pytest
from cp_helpers import make_bank_operation, make_counterparty, make_invoice, make_wallet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin_payout_split import _payer_wallet

from app.models import (
    BusinessOwner,
    CashflowTransaction,
    DdsArticle,
    InvoicePaymentAllocation,
    SupplierPrepayment,
)
from app.services import owner_analytics, supplier_prepayments
from app.services.banking.classifier import (
    OperationSplitLine,
    _drop_untouched_bank_prepayments,
    apply_operation_split,
)
from app.services.counterparty_balance_as_of import build_balance_as_of
from app.services.counterparty_settlement_ledger import ROW_REFUND, build_ledger


@pytest.mark.parametrize("legacy_kind", [False, True])
@pytest.mark.parametrize(
    ("return_code", "returned", "expected", "excess"),
    [
        (supplier_prepayments.SUPPLIER_REFUND_ARTICLE_CODE, "100", "200", "50"),
        (supplier_prepayments.SUPPLIER_REFUND_ARTICLE_CODE, "30", "220", "0"),
        (owner_analytics.OWNER_LOAN_RETURN_ARTICLE_CODE, "300", "50", "100"),
        (owner_analytics.OWNER_LOAN_RETURN_ARTICLE_CODE, "50", "200", "0"),
    ],
)
async def test_owner_loan_returns_match_balance_and_ledger(
    async_session_factory: async_sessionmaker[AsyncSession],
    legacy_kind: bool,
    return_code: str,
    returned: str,
    expected: str,
    excess: str,
) -> None:
    async with async_session_factory() as session:
        owner = await make_counterparty(
            session, name="Павел", cp_type="individual", relationship="informal"
        )
        session.add(
            BusinessOwner(
                counterparty_id=owner.id,
                share_percent=Decimal("50"),
                started_on=date(2026, 1, 1),
            )
        )
        wallet = await make_wallet(session, code="owner-read-side", name="Сейф")
        loan_article = await session.scalar(
            select(DdsArticle).where(
                DdsArticle.code == owner_analytics.OWNER_LOAN_ISSUE_ARTICLE_CODE
            )
        )
        return_article = await session.scalar(
            select(DdsArticle).where(DdsArticle.code == return_code)
        )
        assert loan_article is not None and return_article is not None
        service_article = DdsArticle(
            code="owner-read-service",
            name="Ремонт у собственника",
            movement_type="outflow",
            activity_type="operating",
        )
        session.add(service_article)
        await session.flush()
        for amount, kind, article_id in (
            ("200", "subscription" if legacy_kind else "owner_loan", loan_article.id),
            ("40", "goods", None),
            # FALSE и SQL NULL должны быть одним non-loan бюджетом возврата.
            ("10", "goods", service_article.id),
        ):
            txn = CashflowTransaction(
                counterparty_id=owner.id,
                wallet_id=wallet.id,
                direction="out",
                amount=Decimal(amount),
                operation_date=date(2026, 7, 5),
                article_id=article_id,
                source_kind="manual",
                quality_status="final",
            )
            session.add(txn)
            await session.flush()
            session.add(
                SupplierPrepayment(
                    counterparty_id=owner.id,
                    wallet_id=wallet.id,
                    cashflow_transaction_id=txn.id,
                    article_id=article_id,
                    kind=kind,
                    amount=Decimal(amount),
                    amount_settled=Decimal("0"),
                    status="open",
                    service_period_status="missing",
                )
            )
        session.add(
            CashflowTransaction(
                counterparty_id=owner.id,
                wallet_id=wallet.id,
                direction="in",
                amount=Decimal(returned),
                operation_date=date(2026, 8, 1),
                article_id=return_article.id,
                source_kind="new_payment_income",
                quality_status="final",
            )
        )
        await session.flush()
        await supplier_prepayments.resync_counterparty_refunds(session, owner.id)

        before = await build_balance_as_of(session, as_of=date(2026, 7, 31))
        after = await build_balance_as_of(session, as_of=date(2026, 8, 2))
        assert before.receivable_total == Decimal("250")
        assert after.receivable_total == Decimal(expected)
        assert after.payable_total == Decimal("0")
        ledger = await build_ledger(session, owner.id, today=date(2026, 8, 2))
        assert ledger.closing_balance == Decimal(expected)
        refunds = [row for row in ledger.rows if row.kind == ROW_REFUND]
        assert len(refunds) == 1
        assert refunds[0].uncovered == Decimal(excess)
        assert refunds[0].owner_settlement == (
            return_code == owner_analytics.OWNER_LOAN_RETURN_ARTICLE_CODE
        )


@pytest.mark.parametrize("change_owner", [False, True])
async def test_repeated_bank_split_after_partial_owner_loan_return(
    async_session_factory: async_sessionmaker[AsyncSession], change_owner: bool
) -> None:
    """Повторный разбор возвращённого частично займа не оставляет старую ДЗ сиротой."""
    async with async_session_factory() as session:
        account, wallet = await _payer_wallet(session)
        owners = []
        for name in ("Павел", "Григорий"):
            owner = await make_counterparty(
                session, name=name, cp_type="individual", relationship="informal"
            )
            session.add(
                BusinessOwner(
                    counterparty_id=owner.id,
                    share_percent=Decimal("50"),
                    started_on=date(2026, 1, 1),
                )
            )
            owners.append(owner)
        issue = await session.scalar(
            select(DdsArticle).where(
                DdsArticle.code == owner_analytics.OWNER_LOAN_ISSUE_ARTICLE_CODE
            )
        )
        returned = await session.scalar(
            select(DdsArticle).where(
                DdsArticle.code == owner_analytics.OWNER_LOAN_RETURN_ARTICLE_CODE
            )
        )
        assert issue is not None and returned is not None
        operation = await make_bank_operation(
            session, amount="200", account_id=account.id, operation_date=date(2026, 7, 5)
        )
        await apply_operation_split(
            session,
            operation,
            splits=[OperationSplitLine(issue.id, Decimal("200"), counterparty_id=owners[0].id)],
        )
        session.add(
            CashflowTransaction(
                counterparty_id=owners[0].id,
                wallet_id=wallet.id,
                direction="in",
                amount=Decimal("50"),
                operation_date=date(2026, 8, 1),
                article_id=returned.id,
                source_kind="new_payment_income",
                quality_status="final",
            )
        )
        await session.flush()
        await supplier_prepayments.resync_counterparty_refunds(session, owners[0].id)
        target = owners[1] if change_owner else owners[0]
        await apply_operation_split(
            session,
            operation,
            splits=[OperationSplitLine(issue.id, Decimal("200"), counterparty_id=target.id)],
        )
        loans = list(
            (
                await session.scalars(
                    select(SupplierPrepayment).where(
                        SupplierPrepayment.counterparty_id.in_([owner.id for owner in owners])
                    )
                )
            ).all()
        )
        assert len(loans) == 1
        assert loans[0].cashflow_transaction_id is not None
        assert loans[0].counterparty_id == target.id
        assert loans[0].amount_settled == Decimal("0" if change_owner else "50")
        balance = await build_balance_as_of(session, as_of=date(2026, 8, 2))
        assert balance.receivable_total == Decimal("200" if change_owner else "150")


@pytest.mark.parametrize("closure", ["allocation", "writeoff", "legacy_writeoff"])
async def test_bank_loan_cleanup_preserves_document_and_writeoff_history(
    async_session_factory: async_sessionmaker[AsyncSession], closure: str
) -> None:
    async with async_session_factory() as session:
        owner = await make_counterparty(session, name="Павел", relationship="informal")
        wallet = await make_wallet(session, code="owner-closed-loan", name="Сейф")
        txn = CashflowTransaction(
            counterparty_id=owner.id,
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal("200"),
            operation_date=date(2026, 7, 5),
            source_kind="bank_operation",
            quality_status="final",
        )
        session.add(txn)
        await session.flush()
        loan = SupplierPrepayment(
            counterparty_id=owner.id,
            wallet_id=wallet.id,
            cashflow_transaction_id=txn.id,
            kind=owner_analytics.OWNER_LOAN_KIND,
            amount=Decimal("200"),
            amount_settled=Decimal("50" if closure == "allocation" else "200"),
            status="partially_settled" if closure == "allocation" else "settled",
            settled_on=date(2026, 7, 20) if closure == "writeoff" else None,
        )
        session.add(loan)
        await session.flush()
        if closure == "allocation":
            invoice = await make_invoice(session, counterparty_id=owner.id, amount="200")
            session.add(
                InvoicePaymentAllocation(
                    invoice_id=invoice.id,
                    prepayment_id=loan.id,
                    source_kind="prepayment",
                    amount=Decimal("50"),
                )
            )
            await session.flush()
        with pytest.raises(ValueError, match="Заём уже зачтён"):
            await _drop_untouched_bank_prepayments(session, {txn.id})
        assert (
            await session.scalar(
                select(func.count(SupplierPrepayment.id)).where(SupplierPrepayment.id == loan.id)
            )
            == 1
        )

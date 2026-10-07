"""Выбор получателя займа и отдельный долг собственника во всех денежных каналах."""

import asyncio
from datetime import date, timedelta
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty, make_invoice
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin_payout_split import _payer_wallet, _safe_wallet

from app.models import (
    BusinessOwner,
    CashflowTransaction,
    Counterparty,
    CounterpartyPayableProfile,
    DdsArticle,
    InvoicePaymentAllocation,
    SafeAllocation,
    SupplierPrepayment,
    Wallet,
)
from app.schemas.dds import NewPaymentContextRead
from app.services.bank_payment_status import apply_payment_status
from app.services.banking.cashflow_classify import CashflowSplitLine, apply_cashflow_split
from app.services.banking.safe_allocations import create_allocation, pay_allocation
from app.services.counterparty_payments import (
    CounterpartyPaymentError,
    ExpenseLineInput,
    create_expense_payment_draft,
)
from app.services.new_payment import build_new_payment_context
from app.services.owner_analytics import (
    OWNER_LOAN_ISSUE_ARTICLE_CODE,
    OWNER_LOAN_KIND,
    OWNER_LOAN_RETURN_ARTICLE_CODE,
)
from app.services.supplier_prepayments import (
    SUPPLIER_REFUND_ARTICLE_CODE,
    apply_closing_document,
    ensure_prepayment_from_bank_transaction,
    resync_counterparty_refunds,
    sync_manual_payment_receivable,
)

AMOUNT = Decimal("30000.00")


async def _owner(session: AsyncSession, *, name="Павел", profile=True, **kwargs):
    if profile:
        person = await make_counterparty(
            session,
            name=name,
            inn=None,
            cp_type="individual",
            role=None,
            relationship="informal",
            **kwargs,
        )
    else:
        person = Counterparty(name=name, type="individual", status="active")
        session.add(person)
        await session.flush()
    registration = BusinessOwner(
        counterparty_id=person.id, share_percent=Decimal("50"), started_on=date(2026, 1, 1)
    )
    session.add(registration)
    await session.flush()
    return person, registration


async def _loan_article(session):
    article = await session.scalar(
        select(DdsArticle).where(DdsArticle.code == OWNER_LOAN_ISSUE_ARTICLE_CODE)
    )
    assert article is not None and article.owner_required
    return article


async def test_context_exposes_registry_owners_and_keeps_required_flag(
    async_session_factory: async_sessionmaker[AsyncSession],
):
    async with async_session_factory() as session:
        pavel, _ = await _owner(session, profile=False)
        former, former_registration = await _owner(session, name="Бывший")
        former_registration.ended_on = date(2026, 9, 1)
        archived, _ = await _owner(session, name="Архивный", status="archived")
        stranger = await make_counterparty(session, name="Только роль", role="owner")
        service_owner, _ = await _owner(session, name="Григорий")
        profile = await session.scalar(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == service_owner.id
            )
        )
        profile.service_period_required = True
        await session.flush()
        context = await build_new_payment_context(
            session, permissions=frozenset({"finance.safe.allocate", "finance.safe.confirm_paid"})
        )
        # response_model must preserve both fields, not silently discard them.
        output = NewPaymentContextRead.model_validate(context)
        owners = {row.counterparty_id: row for row in output.owners}
        assert set(owners) == {pavel.id, service_owner.id}
        assert not {former.id, archived.id, stranger.id}.intersection(owners)
        assert owners[pavel.id].relationship == "informal"
        assert not owners[pavel.id].has_requisites
        assert not owners[service_owner.id].service_period_required
        assert next(
            row for row in output.articles if row.code == OWNER_LOAN_ISSUE_ARTICLE_CODE
        ).owner_required
        assert next(
            row for row in output.articles if row.code == OWNER_LOAN_RETURN_ARTICLE_CODE
        ).owner_required


@pytest.mark.parametrize("location", ["safe", "kassa"])
async def test_cash_loan_creates_separate_debt_without_service_profile(
    async_session_factory: async_sessionmaker[AsyncSession],
    location: str,
):
    async with async_session_factory() as session:
        owner, _ = await _owner(session, profile=False)
        loan = await _loan_article(session)
        wallet = Wallet(
            code=f"owner_loan_{location}",
            name=location,
            type="cash_safe" if location == "safe" else "store_cash",
        )
        session.add(wallet)
        await session.flush()
        allocation = await create_allocation(
            session,
            wallet_id=wallet.id,
            amount=AMOUNT,
            free_amount=None,
            article_id=loan.id,
            counterparty_id=owner.id,
            location=location,
        )
        txn_id = await pay_allocation(
            session,
            allocation,
            amount=AMOUNT,
            operation_date=date.today(),
            source_kind="safe_payout" if location == "safe" else "kassa_target_payout",
        )
        prepayment = await session.scalar(
            select(SupplierPrepayment).where(SupplierPrepayment.cashflow_transaction_id == txn_id)
        )
        assert prepayment is not None
        assert (prepayment.counterparty_id, prepayment.kind, prepayment.amount) == (
            owner.id,
            OWNER_LOAN_KIND,
            AMOUNT,
        )
        txn = await session.get(CashflowTransaction, txn_id)
        await sync_manual_payment_receivable(session, txn)
        assert list(
            (
                await session.scalars(
                    select(SupplierPrepayment).where(
                        SupplierPrepayment.cashflow_transaction_id == txn_id
                    )
                )
            ).all()
        ) == [prepayment]
        txn.quality_status = "excluded"
        await sync_manual_payment_receivable(session, txn)
        assert (
            await session.scalar(
                select(SupplierPrepayment.id).where(
                    SupplierPrepayment.cashflow_transaction_id == txn_id
                )
            )
            is None
        )


async def test_bank_via_safe_keeps_profileless_owner_until_actual_payout(
    async_session_factory: async_sessionmaker[AsyncSession],
):
    async with async_session_factory() as session:
        await _payer_wallet(session)
        await _safe_wallet(session)
        owner, _ = await _owner(session, profile=False)
        loan = await _loan_article(session)
        draft = await create_expense_payment_draft(
            session,
            lines=[
                ExpenseLineInput(
                    article_id=loan.id,
                    amount=AMOUNT,
                    counterparty_id=owner.id,
                )
            ],
        )
        assert draft.pays_via_safe
        await apply_payment_status(session, draft=draft, raw_status="executed")
        allocation = await session.scalar(
            select(SafeAllocation).where(SafeAllocation.source_draft_id == draft.id)
        )
        assert allocation is not None and allocation.counterparty_id == owner.id
        assert (
            await session.scalar(
                select(SupplierPrepayment.id).where(SupplierPrepayment.counterparty_id == owner.id)
            )
            is None
        )  # bank transfer only replenished safe, loan has not been issued yet.
        txn_id = await pay_allocation(
            session, allocation, amount=AMOUNT, operation_date=date.today()
        )
        prepayment = await session.scalar(
            select(SupplierPrepayment).where(SupplierPrepayment.cashflow_transaction_id == txn_id)
        )
        assert prepayment is not None and prepayment.kind == OWNER_LOAN_KIND
        assert prepayment.amount == AMOUNT


async def test_direct_bank_loan_ignores_service_period_and_cannot_pay_service_kz(
    async_session_factory: async_sessionmaker[AsyncSession],
):
    async with async_session_factory() as session:
        await _payer_wallet(session)
        owner, _ = await _owner(session)
        profile = await session.scalar(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == owner.id
            )
        )
        profile.relationship = "official"
        profile.requisites = {
            "recipientName": owner.name,
            "inn": "616100000001",
            "bankAcnt": "40802810000000000001",
            "bankBik": "044525225",
            "recipientCorrAccountNumber": "30101810400000000225",
        }
        profile.requisites_verified = True
        profile.service_period_required = True
        act = await make_invoice(
            session,
            counterparty_id=owner.id,
            amount="20000",
            operational_scope="finance",
            invoice_date=date.today(),
        )
        loan = await _loan_article(session)
        draft = await create_expense_payment_draft(
            session,
            lines=[
                ExpenseLineInput(
                    article_id=loan.id,
                    amount=AMOUNT,
                    counterparty_id=owner.id,
                )
            ],
        )
        assert not draft.pays_via_safe and draft.counterparty_id == owner.id
        await apply_payment_status(session, draft=draft, raw_status="executed")
        prepayment = await session.scalar(
            select(SupplierPrepayment).where(SupplierPrepayment.counterparty_id == owner.id)
        )
        assert prepayment is not None and prepayment.kind == OWNER_LOAN_KIND
        assert prepayment.amount == AMOUNT and prepayment.amount_settled == 0
        assert act.payment_status == "unpaid"
        assert (
            await session.scalar(
                select(InvoicePaymentAllocation.id).where(
                    InvoicePaymentAllocation.invoice_id == act.id
                )
            )
            is None
        )
        # A later closing document must also keep its hands off an owner's loan.
        await apply_closing_document(session, act)
        assert prepayment.amount_settled == 0


async def test_loan_returns_only_repay_loans_and_follow_changes_to_money(
    async_session_factory: async_sessionmaker[AsyncSession],
):
    async with async_session_factory() as session:
        owner, _ = await _owner(session)
        loan = await _loan_article(session)
        wallet = Wallet(code="owner_loan_repayment", name="Сейф", type="cash_safe")
        session.add(wallet)
        await session.flush()
        txn = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=AMOUNT,
            operation_date=date.today(),
            article_id=loan.id,
            counterparty_id=owner.id,
            source_kind="manual",
            quality_status="final",
        )
        session.add(txn)
        await session.flush()
        debt = await ensure_prepayment_from_bank_transaction(session, txn)
        service_advance = SupplierPrepayment(
            counterparty_id=owner.id,
            kind="subscription",
            wallet_id=wallet.id,
            amount=Decimal("8000"),
            amount_settled=0,
            status="open",
        )
        session.add(service_advance)
        return_article = await session.scalar(
            select(DdsArticle).where(DdsArticle.code == OWNER_LOAN_RETURN_ARTICLE_CODE)
        )
        supplier_return = await session.scalar(
            select(DdsArticle).where(DdsArticle.code == SUPPLIER_REFUND_ARTICLE_CODE)
        )
        repayment = CashflowTransaction(
            wallet_id=wallet.id,
            direction="in",
            amount=Decimal("10000"),
            operation_date=date.today(),
            article_id=return_article.id,
            counterparty_id=owner.id,
            source_kind="new_payment_income",
            quality_status="final",
        )
        ordinary_refund = CashflowTransaction(
            wallet_id=wallet.id,
            direction="in",
            amount=Decimal("8000"),
            operation_date=date.today(),
            article_id=supplier_return.id,
            counterparty_id=owner.id,
            source_kind="new_payment_income",
            quality_status="final",
        )
        session.add_all([repayment, ordinary_refund])
        await session.flush()
        await resync_counterparty_refunds(session, owner.id)
        assert debt.amount_settled == Decimal("10000")
        assert service_advance.amount_settled == Decimal("8000")
        await resync_counterparty_refunds(session, owner.id)
        assert debt.amount_settled == Decimal("10000")
        repayment.quality_status = "excluded"
        await resync_counterparty_refunds(session, owner.id)
        assert debt.amount_settled == 0
        assert service_advance.amount_settled == Decimal("8000")
        # Moving/excluding issuance must leave no stale debt or repayment allocation.
        txn.quality_status = "excluded"
        await sync_manual_payment_receivable(session, txn)
        assert (
            await session.scalar(
                select(SupplierPrepayment.id).where(
                    SupplierPrepayment.cashflow_transaction_id == txn.id
                )
            )
            is None
        )


@pytest.mark.parametrize("location", ["safe", "kassa"])
def test_cash_payment_api_accepts_profileless_owner_and_posts_repayment(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
    location: str,
):
    async def seed():
        async with async_session_factory() as session:
            owner, _ = await _owner(session, profile=False)
            loan = await _loan_article(session)
            repayment = await session.scalar(
                select(DdsArticle).where(DdsArticle.code == OWNER_LOAN_RETURN_ARTICLE_CODE)
            )
            wallet = Wallet(
                code=f"owner_api_{location}",
                name=location,
                type="cash_safe" if location == "safe" else "store_cash",
                opening_balance=Decimal("50000"),
                opening_balance_date=date.today() - timedelta(days=1),
            )
            session.add(wallet)
            await session.flush()
            ids = (str(owner.id), str(loan.id), str(repayment.id), str(wallet.id))
            await session.commit()
            return ids

    owner_id, loan_id, return_id, wallet_id = asyncio.run(seed())
    response = client.post(
        "/api/v1/dds/new-payment/expense-cash",
        headers={"X-User-Role": "admin"},
        json={
            "wallet_id": wallet_id,
            "pay_now": True,
            "lines": [{"article_id": loan_id, "amount": "30000", "counterparty_id": owner_id}],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["paid"]
    response = client.post(
        "/api/v1/dds/new-payment/income-cash",
        headers={"X-User-Role": "admin"},
        json={
            "wallet_id": wallet_id,
            "lines": [{"article_id": return_id, "amount": "10000", "counterparty_id": owner_id}],
        },
    )
    assert response.status_code == 201, response.text

    async def read():
        async with async_session_factory() as session:
            rows = (
                await session.scalars(
                    select(SupplierPrepayment).where(SupplierPrepayment.counterparty_id == owner_id)
                )
            ).all()
            return [(row.kind, row.amount, row.amount_settled) for row in rows]

    assert asyncio.run(read()) == [(OWNER_LOAN_KIND, AMOUNT, Decimal("10000"))]


@pytest.mark.parametrize("recipient", ["missing", "stranger"])
def test_cash_income_api_rejects_owner_article_without_registered_owner(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
    recipient: str,
):
    async def seed():
        async with async_session_factory() as session:
            stranger = await make_counterparty(session, name="Поставщик", role="owner")
            repayment = await session.scalar(
                select(DdsArticle).where(DdsArticle.code == OWNER_LOAN_RETURN_ARTICLE_CODE)
            )
            wallet = Wallet(code="owner_income_validation", name="Касса", type="store_cash")
            session.add(wallet)
            await session.flush()
            ids = (str(stranger.id), str(repayment.id), str(wallet.id))
            await session.commit()
            return ids

    stranger_id, return_id, wallet_id = asyncio.run(seed())
    line = {"article_id": return_id, "amount": "10000"}
    if recipient == "stranger":
        line["counterparty_id"] = stranger_id
    response = client.post(
        "/api/v1/dds/new-payment/income-cash",
        headers={"X-User-Role": "admin"},
        json={"wallet_id": wallet_id, "lines": [line]},
    )
    assert response.status_code == 422, response.text
    assert "собственник" in response.json()["detail"].lower()


async def _fully_allocated_service_payment(session):
    owner, _ = await _owner(session)
    wallet = Wallet(code="owner_loan_reclassification", name="Сейф", type="cash_safe")
    session.add(wallet)
    service_article = await session.scalar(
        select(DdsArticle).where(DdsArticle.code == "prochie_rashody")
    )
    act = await make_invoice(
        session,
        counterparty_id=owner.id,
        amount=AMOUNT,
        operational_scope="finance",
        invoice_date=date.today(),
    )
    await session.flush()
    txn = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=AMOUNT,
        operation_date=date.today(),
        article_id=service_article.id,
        counterparty_id=owner.id,
        source_kind="manual",
        quality_status="final",
    )
    session.add(txn)
    await session.flush()
    assert await ensure_prepayment_from_bank_transaction(session, txn) is None
    assert act.payment_status == "paid"
    return owner, service_article, act, txn


async def test_manual_reclassification_into_loan_releases_automatic_service_payment(
    async_session_factory: async_sessionmaker[AsyncSession],
):
    async with async_session_factory() as session:
        owner, _service, act, txn = await _fully_allocated_service_payment(session)
        loan = await _loan_article(session)
        txn.article_id = loan.id
        debt = await sync_manual_payment_receivable(session, txn)
        assert debt is not None and debt.kind == OWNER_LOAN_KIND
        assert debt.counterparty_id == owner.id and debt.amount == AMOUNT
        assert act.payment_status == "unpaid"
        assert (
            await session.scalar(
                select(InvoicePaymentAllocation.id).where(
                    InvoicePaymentAllocation.invoice_id == act.id
                )
            )
            is None
        )


@pytest.mark.parametrize("loan_first", [True, False])
async def test_manual_split_with_loan_never_keeps_old_full_service_allocation(
    async_session_factory: async_sessionmaker[AsyncSession],
    loan_first: bool,
):
    async with async_session_factory() as session:
        owner, service, act, txn = await _fully_allocated_service_payment(session)
        loan = await _loan_article(session)
        lines = [
            CashflowSplitLine(loan.id, Decimal("20000"), counterparty_id=owner.id),
            CashflowSplitLine(service.id, Decimal("10000"), counterparty_id=owner.id),
        ]
        await apply_cashflow_split(
            session, txn, splits=lines if loan_first else list(reversed(lines))
        )
        debts = (
            await session.scalars(
                select(SupplierPrepayment).where(SupplierPrepayment.counterparty_id == owner.id)
            )
        ).all()
        assert [(row.kind, row.amount) for row in debts] == [(OWNER_LOAN_KIND, Decimal("20000"))]
        allocations = (
            await session.scalars(
                select(InvoicePaymentAllocation).where(
                    InvoicePaymentAllocation.invoice_id == act.id
                )
            )
        ).all()
        assert sum((row.amount for row in allocations), Decimal("0")) == Decimal("10000")
        assert act.payment_status == "partially_paid"


async def test_loan_reclassification_refuses_explicit_document_payment(
    async_session_factory: async_sessionmaker[AsyncSession],
):
    async with async_session_factory() as session:
        _owner_row, _service, act, txn = await _fully_allocated_service_payment(session)
        allocation = await session.scalar(
            select(InvoicePaymentAllocation).where(InvoicePaymentAllocation.invoice_id == act.id)
        )
        allocation.origin = "manual"
        loan = await _loan_article(session)
        txn.article_id = loan.id
        with pytest.raises(CounterpartyPaymentError, match="закреплён за документом"):
            await sync_manual_payment_receivable(session, txn)
        assert act.payment_status == "paid"
        assert allocation.amount == AMOUNT
        assert (
            await session.scalar(
                select(SupplierPrepayment.id).where(
                    SupplierPrepayment.cashflow_transaction_id == txn.id
                )
            )
            is None
        )


@pytest.mark.parametrize("legacy_kind", [OWNER_LOAN_KIND, "subscription"])
async def test_loan_returns_do_not_revive_legacy_written_off_debt(
    async_session_factory: async_sessionmaker[AsyncSession],
    legacy_kind: str,
):
    async with async_session_factory() as session:
        owner, _ = await _owner(session)
        loan = await _loan_article(session)
        repayment_article = await session.scalar(
            select(DdsArticle).where(DdsArticle.code == OWNER_LOAN_RETURN_ARTICLE_CODE)
        )
        wallet = Wallet(code="owner_loan_legacy_writeoff", name="Сейф", type="cash_safe")
        session.add(wallet)
        await session.flush()
        written_off = SupplierPrepayment(
            counterparty_id=owner.id,
            kind=legacy_kind,
            wallet_id=wallet.id,
            article_id=loan.id,
            amount=AMOUNT,
            amount_settled=AMOUNT,
            status="settled",
            settled_on=None,
        )
        session.add(written_off)
        await session.flush()
        current_debt = SupplierPrepayment(
            counterparty_id=owner.id,
            kind=OWNER_LOAN_KIND,
            wallet_id=wallet.id,
            article_id=loan.id,
            amount=Decimal("5000"),
            amount_settled=0,
            status="open",
        )
        repayment = CashflowTransaction(
            wallet_id=wallet.id,
            direction="in",
            amount=Decimal("1000"),
            operation_date=date.today(),
            article_id=repayment_article.id,
            counterparty_id=owner.id,
            source_kind="new_payment_income",
            quality_status="final",
        )
        session.add_all([current_debt, repayment])
        await session.flush()
        for _ in range(2):
            await resync_counterparty_refunds(session, owner.id)
            assert written_off.status == "settled"
            assert written_off.amount_settled == AMOUNT and written_off.settled_on is None
            assert current_debt.amount_settled == Decimal("1000")
        repayment.quality_status = "excluded"
        await resync_counterparty_refunds(session, owner.id)
        assert written_off.status == "settled" and written_off.amount_settled == AMOUNT
        assert current_debt.status == "open" and current_debt.amount_settled == 0

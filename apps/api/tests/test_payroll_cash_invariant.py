from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_payroll_payments import create_actor_user, create_payroll_run

from app.models import (
    CashflowTransaction,
    DdsArticle,
    PayrollPayment,
    PayrollPayoutBooking,
    SafeAllocation,
    Wallet,
)
from app.services.payroll_payments import (
    PayrollConflictError,
    mark_payments_selected,
    unmark_payment,
)
from app.services.payroll_payouts import PAYROLL_PAYOUT_SOURCE_KIND

pytestmark = pytest.mark.asyncio


async def _fund_wallet(session: AsyncSession, code: str, amount: Decimal) -> Wallet:
    wallet = await session.scalar(select(Wallet).where(Wallet.code == code))
    assert wallet is not None
    wallet.opening_balance = Decimal("0")
    session.add(
        CashflowTransaction(
            wallet_id=wallet.id,
            direction="in",
            amount=amount,
            operation_date=date(2026, 7, 21),
            source_kind="test_funding",
            payment_purpose="Тестовое пополнение",
            quality_status="final",
        )
    )
    await session.commit()
    return wallet


async def _reserve_payroll(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    wallet: Wallet,
    amount: Decimal,
) -> SafeAllocation:
    article = await session.scalar(
        select(DdsArticle).where(DdsArticle.code == "zarplata_proizvodstvennogo_personala")
    )
    assert article is not None
    reserve = SafeAllocation(
        wallet_id=wallet.id,
        amount=amount,
        amount_paid=Decimal("0"),
        article_id=article.id,
        purpose="Выплата зарплаты производственному персоналу",
        source_run_id=run_id,
        status="reserved",
        location="kassa" if wallet.code == "tk_chernikova" else "safe",
    )
    session.add(reserve)
    await session.commit()
    return reserve


async def test_selected_payout_uses_chosen_wallet_and_unmark_restores_balance(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session, employee_line_totals=[[Decimal("756.29")]]
        )
        wallet = await _fund_wallet(session, "tk_chernikova", Decimal("1000"))
        await _reserve_payroll(session, run_id=run.id, wallet=wallet, amount=Decimal("756.29"))

        marked = await mark_payments_selected(
            session,
            run.id,
            [employees[0].id],
            paid_at=date(2026, 7, 21),
            cash_wallet_code="tk_chernikova",
            actor_user_id=actor.id,
        )
        assert marked == 1
        booking = await session.scalar(
            select(PayrollPayoutBooking).where(PayrollPayoutBooking.run_id == run.id)
        )
        assert booking is not None
        expense = await session.get(CashflowTransaction, booking.cashflow_transaction_id)
        assert expense is not None
        assert expense.wallet_id == wallet.id
        assert expense.direction == "out"
        assert expense.amount == Decimal("756.29")

        await unmark_payment(session, run.id, employees[0].id, actor_user_id=actor.id)

        await session.refresh(booking)
        reversal = await session.get(CashflowTransaction, booking.reversal_transaction_id)
        assert reversal is not None
        assert reversal.wallet_id == wallet.id
        assert reversal.direction == "in"
        assert reversal.amount == expense.amount
        assert (
            await session.scalar(select(PayrollPayment).where(PayrollPayment.run_id == run.id))
            is None
        )
        net = await session.scalar(
            select(
                func.sum(
                    case(
                        (CashflowTransaction.direction == "in", CashflowTransaction.amount),
                        else_=-CashflowTransaction.amount,
                    )
                )
            ).where(CashflowTransaction.wallet_id == wallet.id)
        )
        assert Decimal(net or 0) == Decimal("1000.00")


async def test_payout_with_insufficient_funds_is_atomic(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session, employee_line_totals=[[Decimal("756.29")]]
        )
        wallet = await _fund_wallet(session, "cash_safe", Decimal("100"))
        await _reserve_payroll(session, run_id=run.id, wallet=wallet, amount=Decimal("756.29"))
        run_id = run.id
        employee_id = employees[0].id
        wallet_id = wallet.id

        with pytest.raises(PayrollConflictError, match="доступно"):
            await mark_payments_selected(
                session,
                run_id,
                [employee_id],
                paid_at=date(2026, 7, 21),
                cash_wallet_code="cash_safe",
                actor_user_id=actor.id,
            )
        await session.rollback()

        assert (
            await session.scalar(select(PayrollPayment).where(PayrollPayment.run_id == run_id))
            is None
        )
        payout_count = await session.scalar(
            select(func.count(CashflowTransaction.id)).where(
                CashflowTransaction.wallet_id == wallet_id,
                CashflowTransaction.source_kind == PAYROLL_PAYOUT_SOURCE_KIND,
            )
        )
        assert payout_count == 0


@pytest.mark.parametrize("wallet_code", ["cash_safe", "tk_chernikova"])
async def test_free_cash_cannot_fund_payroll_without_run_reserve(
    async_session_factory: async_sessionmaker[AsyncSession],
    wallet_code: str,
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session, employee_line_totals=[[Decimal("8150.00")]]
        )
        wallet = await _fund_wallet(session, wallet_code, Decimal("9000.00"))
        run_id = run.id
        wallet_id = wallet.id

        with pytest.raises(PayrollConflictError, match="нет активного резерва"):
            await mark_payments_selected(
                session,
                run_id,
                [employees[0].id],
                paid_at=date(2026, 7, 21),
                cash_wallet_code=wallet_code,
                actor_user_id=actor.id,
            )
        await session.rollback()

        assert (
            await session.scalar(select(PayrollPayment).where(PayrollPayment.run_id == run_id))
            is None
        )
        assert (
            await session.scalar(
                select(func.count(CashflowTransaction.id)).where(
                    CashflowTransaction.wallet_id == wallet_id,
                    CashflowTransaction.source_kind == PAYROLL_PAYOUT_SOURCE_KIND,
                )
            )
            == 0
        )


async def test_free_balance_cannot_cover_amount_above_payroll_reserve(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session, employee_line_totals=[[Decimal("8150.00")]]
        )
        wallet = await _fund_wallet(session, "cash_safe", Decimal("9000.00"))
        await _reserve_payroll(session, run_id=run.id, wallet=wallet, amount=Decimal("5000.00"))

        with pytest.raises(PayrollConflictError, match="зарплатном резерве.*5000.00"):
            await mark_payments_selected(
                session,
                run.id,
                [employees[0].id],
                paid_at=date(2026, 7, 21),
                cash_wallet_code="cash_safe",
                actor_user_id=actor.id,
            )


async def test_payroll_rejects_any_other_cash_wallet(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(session)

        with pytest.raises(PayrollConflictError, match="только Сейф или торговую кассу"):
            await mark_payments_selected(
                session,
                run.id,
                [employees[0].id],
                paid_at=date(2026, 7, 21),
                cash_wallet_code="tbank_main",
                actor_user_id=actor.id,
            )


async def test_unmark_reconstructs_legacy_aggregate_payout_link(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session, employee_line_totals=[[Decimal("756.29")]]
        )
        wallet = await session.scalar(select(Wallet).where(Wallet.code == "cash_safe"))
        article = await session.scalar(
            select(DdsArticle).where(DdsArticle.code == "zarplata_proizvodstvennogo_personala")
        )
        assert wallet is not None and article is not None
        payment = PayrollPayment(
            run_id=run.id,
            employee_id=employees[0].id,
            amount=Decimal("756.29"),
            amount_cash=Decimal("0"),
            amount_account=Decimal("756.29"),
            booked_amount=Decimal("756.29"),
            status="paid",
            paid_at=date(2026, 7, 21),
            paid_by_user_id=actor.id,
        )
        session.add(payment)
        await session.flush()
        legacy_expense = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal("756.29"),
            operation_date=date(2026, 7, 21),
            article_id=article.id,
            source_kind=PAYROLL_PAYOUT_SOURCE_KIND,
            source_id=run.id,
            payment_purpose="Старая агрегированная выплата",
            quality_status="final",
        )
        session.add(legacy_expense)
        await session.commit()

        await unmark_payment(session, run.id, employees[0].id, actor_user_id=actor.id)

        booking = await session.scalar(
            select(PayrollPayoutBooking).where(
                PayrollPayoutBooking.cashflow_transaction_id == legacy_expense.id
            )
        )
        assert booking is not None
        assert booking.reversal_transaction_id is not None
        reversal = await session.get(CashflowTransaction, booking.reversal_transaction_id)
        assert reversal is not None
        assert reversal.wallet_id == wallet.id
        assert reversal.amount == Decimal("756.29")
        assert reversal.direction == "in"

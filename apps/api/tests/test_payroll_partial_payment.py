"""Частичная выплата ведомости: дробление суммы, доплата, инкрементальный ДДС без задвоения."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_payroll_payments import create_actor_user, create_payroll_run

from app.api.deps import CurrentActor
from app.api.v1.routes import payroll as payroll_routes
from app.models import CashflowTransaction, PayrollPayment
from app.services.payroll_payments import (
    PayrollConflictError,
    mark_partial_payment,
    mark_payments_selected,
)
from app.services.payroll_payouts import PAYROLL_PAYOUT_SOURCE_KIND
from app.services.payroll_runner import list_runs

pytestmark = pytest.mark.asyncio

PAID_AT = date(2026, 5, 27)


def _actor(user_id) -> CurrentActor:
    return CurrentActor(roles=frozenset({"manager"}), user_id=user_id)


async def _payment(session: AsyncSession, run_id) -> PayrollPayment | None:
    return await session.scalar(select(PayrollPayment).where(PayrollPayment.run_id == run_id))


async def _dds_out_total(session: AsyncSession, run_id) -> Decimal:
    value = await session.scalar(
        select(func.coalesce(func.sum(CashflowTransaction.amount), 0)).where(
            CashflowTransaction.source_kind == PAYROLL_PAYOUT_SOURCE_KIND,
            CashflowTransaction.source_id == run_id,
            CashflowTransaction.direction == "out",
        )
    )
    return Decimal(value or 0)


async def test_partial_payment_sets_partially_paid(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session, employee_line_totals=[[Decimal("1000")]]
        )
        payment = await mark_partial_payment(
            session, run.id, employees[0].id, amount=Decimal("700"), paid_at=PAID_AT,
            actor_user_id=actor.id,
        )
        assert payment.status == "partially_paid"
        assert payment.amount == Decimal("700.00")

        lines = await payroll_routes.get_lines(run.id, session, _actor(actor.id))
        assert {line.payment_status for line in lines} == {"partially_paid"}
        assert {line.paid_amount for line in lines} == {700.0}


async def test_topup_completes_to_paid(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session, employee_line_totals=[[Decimal("1000")]]
        )
        await mark_partial_payment(
            session, run.id, employees[0].id, amount=Decimal("700"), paid_at=PAID_AT,
            actor_user_id=actor.id,
        )
        payment = await mark_partial_payment(
            session, run.id, employees[0].id, amount=Decimal("300"), paid_at=PAID_AT,
            actor_user_id=actor.id,
        )
        assert payment.status == "paid"
        assert payment.amount == Decimal("1000.00")


async def test_topup_none_pays_full_remaining(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session, employee_line_totals=[[Decimal("1000")]]
        )
        await mark_partial_payment(
            session, run.id, employees[0].id, amount=Decimal("650"), paid_at=PAID_AT,
            actor_user_id=actor.id,
        )
        payment = await mark_partial_payment(
            session, run.id, employees[0].id, amount=None, paid_at=PAID_AT,
            actor_user_id=actor.id,
        )
        assert payment.status == "paid"
        assert payment.amount == Decimal("1000.00")


async def test_partial_over_remaining_returns_409(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session, employee_line_totals=[[Decimal("1000")]]
        )
        await mark_partial_payment(
            session, run.id, employees[0].id, amount=Decimal("700"), paid_at=PAID_AT,
            actor_user_id=actor.id,
        )
        with pytest.raises(PayrollConflictError):
            await mark_partial_payment(
                session, run.id, employees[0].id, amount=Decimal("400"), paid_at=PAID_AT,
                actor_user_id=actor.id,
            )


async def test_partial_on_fully_paid_returns_409(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session, employee_line_totals=[[Decimal("1000")]]
        )
        await mark_partial_payment(
            session, run.id, employees[0].id, amount=None, paid_at=PAID_AT,
            actor_user_id=actor.id,
        )
        with pytest.raises(PayrollConflictError):
            await mark_partial_payment(
                session, run.id, employees[0].id, amount=Decimal("100"), paid_at=PAID_AT,
                actor_user_id=actor.id,
            )


async def test_dds_booked_incrementally_no_double(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Транш и доплата книжат ДДС по дельте — сумма расхода = выплаченному, не задвоено."""
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session, employee_line_totals=[[Decimal("1000")]]
        )
        await mark_partial_payment(
            session, run.id, employees[0].id, amount=Decimal("600"), paid_at=PAID_AT,
            actor_user_id=actor.id,
        )
        assert await _dds_out_total(session, run.id) == Decimal("600.00")
        await mark_partial_payment(
            session, run.id, employees[0].id, amount=Decimal("400"), paid_at=PAID_AT,
            actor_user_id=actor.id,
        )
        assert await _dds_out_total(session, run.id) == Decimal("1000.00")
        payment = await _payment(session, run.id)
        assert payment is not None
        assert payment.booked_amount == Decimal("1000.00")


async def test_partial_then_bulk_no_double_book(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Частично выплаченного сотрудника добивает bulk «Выплатить» — ДДС не задваивается."""
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session, employee_line_totals=[[Decimal("1000")]]
        )
        await mark_partial_payment(
            session, run.id, employees[0].id, amount=Decimal("600"), paid_at=PAID_AT,
            actor_user_id=actor.id,
        )
        await mark_payments_selected(
            session, run.id, [employees[0].id], paid_at=PAID_AT, actor_user_id=actor.id
        )
        assert await _dds_out_total(session, run.id) == Decimal("1000.00")
        payment = await _payment(session, run.id)
        assert payment is not None
        assert payment.status == "paid"
        assert payment.amount == Decimal("1000.00")
        assert payment.booked_amount == Decimal("1000.00")


async def test_runs_list_summary_reports_partial_state(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session, employee_line_totals=[[Decimal("1000")], [Decimal("500")]]
        )
        await mark_partial_payment(
            session, run.id, employees[0].id, amount=Decimal("700"), paid_at=PAID_AT,
            actor_user_id=actor.id,
        )
        runs = await list_runs(session)
        summary = next(item["summary"] for item in runs if item["id"] == run.id)
        assert summary["payment_state"] == "partial"
        assert summary["underpaid_count"] == 1
        assert summary["remaining_shortfall"] == 300.0
        assert summary["paid_total"] == 700.0

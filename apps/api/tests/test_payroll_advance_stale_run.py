"""Детектор устаревания ведомости: выдача аванса ПОСЛЕ расчёта.

`run_has_unaccounted_advances` ловит аванс/заём, заведённый (в т.ч. задним числом) в уже
посчитанную, но не финализированную ведомость — его удержание не материализовано, поэтому
финализация должна блокироваться до пересчёта. Критерий `created_at > run.started_at`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    Employee,
    EmployeePositionAssignment,
    PayrollLine,
    PayrollPeriod,
    PayrollRun,
    SalaryAdvance,
    SalaryAdvanceRecovery,
)
from app.services.payroll_advance_recovery import run_has_unaccounted_advances
from app.services.payroll_runner import PayrollConflictError, finalize_payroll_run

CALC_AT = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
AFTER_CALC = datetime(2026, 7, 1, 13, 0, tzinfo=UTC)
BEFORE_CALC = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)


async def _make_manager(session: AsyncSession) -> Employee:
    employee = Employee(
        id=uuid.uuid4(),
        full_name="Управляющий Тест",
        iiko_id=f"iiko-{uuid.uuid4()}",
        status="active",
        is_senior=False,
        is_deputy_senior=False,
        hire_date=None,
        fire_date=None,
        pin_hash="hashed-pin",
        pin_set_at=datetime(2026, 1, 1, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add(employee)
    await session.flush()
    session.add(
        EmployeePositionAssignment(
            id=uuid.uuid4(),
            employee_id=employee.id,
            position="Управляющий",
            effective_from=date(2026, 1, 1),
            effective_to=None,
        )
    )
    await session.flush()
    return employee


async def _make_admin_run(
    session: AsyncSession, employee: Employee
) -> tuple[PayrollPeriod, PayrollRun]:
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="half_month",
        start_date=date(2026, 6, 16),
        end_date=date(2026, 6, 30),
        payroll_date=date(2026, 7, 1),
        status="open",
    )
    session.add(period)
    await session.flush()
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=period.id,
        started_at=CALC_AT,
        status="completed",
        blocking_issues=[],
        summary={},
    )
    session.add(run)
    await session.flush()
    session.add(
        PayrollLine(
            run_id=run.id,
            employee_id=employee.id,
            role="Управляющий",
            total_payable=Decimal("45000"),
        )
    )
    await session.flush()
    return period, run


def _advance(employee: Employee, *, created_at: datetime) -> SalaryAdvance:
    return SalaryAdvance(
        id=uuid.uuid4(),
        employee_id=employee.id,
        role="Управляющий",
        kind="advance",
        amount=Decimal("5000"),
        per_installment_amount=Decimal("5000"),
        installments_count=1,
        recovered_amount=Decimal("0"),
        status="issued",
        issued_on=date(2026, 6, 26),
        payout_method="cash",
        created_at=created_at,
    )


async def test_run_stale_when_advance_issued_after_calc(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _make_manager(session)
        period, run = await _make_admin_run(session, employee)
        session.add(_advance(employee, created_at=AFTER_CALC))
        await session.commit()

        assert await run_has_unaccounted_advances(session, run, period) is True
        with pytest.raises(PayrollConflictError):
            await finalize_payroll_run(session, run.id)


async def test_run_not_stale_when_advance_accounted(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _make_manager(session)
        period, run = await _make_admin_run(session, employee)
        advance = _advance(employee, created_at=AFTER_CALC)
        session.add(advance)
        await session.flush()
        session.add(
            SalaryAdvanceRecovery(advance_id=advance.id, run_id=run.id, amount=Decimal("5000"))
        )
        await session.commit()

        assert await run_has_unaccounted_advances(session, run, period) is False


async def test_run_not_stale_when_advance_predates_calc(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _make_manager(session)
        period, run = await _make_admin_run(session, employee)
        session.add(_advance(employee, created_at=BEFORE_CALC))
        await session.commit()

        assert await run_has_unaccounted_advances(session, run, period) is False

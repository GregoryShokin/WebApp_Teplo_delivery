"""Регрессии дат выдачи и первого удержания займа в недельных ведомостях."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Employee, PayrollLine, PayrollPeriod, PayrollRun, SalaryAdvance
from app.services.payroll_advance_recovery import (
    apply_advance_issuances,
    apply_advance_recoveries,
)


async def _employee(session: AsyncSession) -> Employee:
    employee = Employee(
        id=uuid.uuid4(),
        full_name="Шевченко Люба Тест",
        iiko_id=f"iiko-{uuid.uuid4()}",
        status="active",
        is_senior=False,
        is_deputy_senior=False,
        pin_hash="hashed-pin",
        pin_set_at=datetime(2026, 1, 1, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add(employee)
    await session.flush()
    return employee


async def _week(
    session: AsyncSession,
    *,
    start: date,
    end: date,
    payout: date,
    employee: Employee,
    payable: str,
) -> tuple[PayrollPeriod, PayrollRun, PayrollLine]:
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="week",
        start_date=start,
        end_date=end,
        payroll_date=payout,
        status="open",
    )
    session.add(period)
    await session.flush()
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=period.id,
        status="completed",
        blocking_issues=[],
        summary={},
    )
    session.add(run)
    await session.flush()
    line = PayrollLine(
        run_id=run.id,
        employee_id=employee.id,
        role="Повар",
        total_payable=Decimal(payable),
    )
    session.add(line)
    await session.flush()
    return period, run, line


async def test_payroll_loan_issued_sep_1_starts_recovery_on_sep_8(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """01.09 заём выдаётся с ведомостью, но не удерживается; 08.09 удерживается доля."""
    async with async_session_factory() as session:
        employee = await _employee(session)
        loan = SalaryAdvance(
            id=uuid.uuid4(),
            employee_id=employee.id,
            role="Повар",
            kind="loan",
            amount=Decimal("10000"),
            per_installment_amount=Decimal("2000"),
            installments_count=5,
            recovered_amount=Decimal("0"),
            status="issued",
            issued_on=date(2026, 9, 1),
            recovery_start_date=date(2026, 9, 8),
            payout_method="payroll",
        )
        session.add(loan)

        first_period, first_run, first_line = await _week(
            session,
            start=date(2026, 8, 25),
            end=date(2026, 8, 31),
            payout=date(2026, 9, 1),
            employee=employee,
            payable="13250",
        )

        first_recovery = await apply_advance_recoveries(
            session, first_period, first_run, [first_line]
        )
        first_issuance = await apply_advance_issuances(
            session, first_period, first_run, [first_line]
        )

        assert first_recovery["advance_recovery_count"] == 0
        assert first_issuance["advance_issued_count"] == 1
        assert first_line.advance_recovered == Decimal("0")
        assert first_line.total_payable == Decimal("23250.00")

        second_period, second_run, second_line = await _week(
            session,
            start=date(2026, 9, 1),
            end=date(2026, 9, 7),
            payout=date(2026, 9, 8),
            employee=employee,
            payable="12000",
        )

        second_recovery = await apply_advance_recoveries(
            session, second_period, second_run, [second_line]
        )
        second_issuance = await apply_advance_issuances(
            session, second_period, second_run, [second_line]
        )

        assert second_recovery["advance_recovery_count"] == 1
        assert second_issuance["advance_issued_count"] == 0
        assert second_line.advance_recovered == Decimal("2000.00")
        assert second_line.total_payable == Decimal("10000.00")

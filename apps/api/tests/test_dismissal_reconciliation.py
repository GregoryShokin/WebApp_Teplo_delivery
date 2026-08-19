from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.models import (
    DepositPayoutSchedule,
    Employee,
    EmployeePayout,
    PayrollLine,
    PayrollPayment,
    PayrollPeriod,
    PayrollRun,
    SalaryAdvance,
    ShiftLedgerEntry,
)
from app.services import dismissal_reconciliation_service


def _dismissing_employee(full_name: str) -> Employee:
    return Employee(
        id=uuid.uuid4(),
        full_name=full_name,
        iiko_id=f"iiko-{uuid.uuid4()}",
        category="category_2",
        status="dismissing",
        fire_date=date(2026, 6, 15),
        is_senior=False,
        is_deputy_senior=False,
        pin_hash="hashed-pin",
        pin_set_at=datetime(2026, 5, 1, tzinfo=UTC),
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
        updated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_reconcile_flips_dismissing_to_inactive_when_fully_settled(
    async_session_factory: Any,
) -> None:
    async with async_session_factory() as session:
        employee = _dismissing_employee("Clean Leaver")
        session.add(employee)
        await session.flush()

        state = await dismissal_reconciliation_service.settlement_state(session, employee.id)
        assert state.fully_settled is True
        assert state.outstanding() == []

        flipped = await dismissal_reconciliation_service.reconcile_dismissing_employee(
            session, employee.id
        )
        assert flipped is True
        assert employee.status == "inactive"


@pytest.mark.asyncio
async def test_reconcile_keeps_dismissing_with_outstanding_loan(
    async_session_factory: Any,
) -> None:
    async with async_session_factory() as session:
        employee = _dismissing_employee("Loan Debtor")
        session.add(employee)
        await session.flush()
        session.add(
            SalaryAdvance(
                id=uuid.uuid4(),
                employee_id=employee.id,
                role="cook",
                kind="loan",
                amount=Decimal("5000.00"),
                per_installment_amount=Decimal("1000.00"),
                installments_count=5,
                recovered_amount=Decimal("1000.00"),
                status="issued",
                issued_on=date(2026, 6, 1),
            )
        )
        await session.flush()

        state = await dismissal_reconciliation_service.settlement_state(session, employee.id)
        assert state.advances_settled is False
        assert state.fully_settled is False
        assert "займы/авансы" in state.outstanding()

        flipped = await dismissal_reconciliation_service.reconcile_dismissing_employee(
            session, employee.id
        )
        assert flipped is False
        assert employee.status == "dismissing"


@pytest.mark.asyncio
async def test_reconcile_keeps_dismissing_with_pending_employee_payout(
    async_session_factory: Any,
) -> None:
    async with async_session_factory() as session:
        employee = _dismissing_employee("Payout Pending")
        session.add(employee)
        await session.flush()
        session.add(
            EmployeePayout(
                id=uuid.uuid4(),
                employee_id=employee.id,
                kind="salary",
                amount=Decimal("3000.00"),
                payout_date=date(2026, 7, 7),
                status="pending",
            )
        )
        await session.flush()

        state = await dismissal_reconciliation_service.settlement_state(session, employee.id)
        assert state.payouts_settled is False
        assert state.fully_settled is False
        assert "разовые выплаты" in state.outstanding()

        flipped = await dismissal_reconciliation_service.reconcile_dismissing_employee(
            session, employee.id
        )
        assert flipped is False
        assert employee.status == "dismissing"


@pytest.mark.asyncio
async def test_reconcile_keeps_dismissing_with_pending_deposit_schedule(
    async_session_factory: Any,
) -> None:
    async with async_session_factory() as session:
        employee = _dismissing_employee("Deposit Pending")
        session.add(employee)
        await session.flush()
        session.add(
            DepositPayoutSchedule(
                id=uuid.uuid4(),
                employee_id=employee.id,
                status="pending",
            )
        )
        await session.flush()

        state = await dismissal_reconciliation_service.settlement_state(session, employee.id)
        assert state.deposit_settled is False
        assert state.fully_settled is False

        flipped = await dismissal_reconciliation_service.reconcile_dismissing_employee(
            session, employee.id
        )
        assert flipped is False
        assert employee.status == "dismissing"


async def _finalized_run(
    session: Any, *, week: int, is_legacy: bool
) -> tuple[PayrollPeriod, PayrollRun]:
    start = date(2026, 5, 19) + timedelta(days=7 * week)
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="week",
        start_date=start,
        end_date=start + timedelta(days=6),
        payroll_date=start + timedelta(days=7),
        status="finalized",
    )
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=period.id,
        started_at=datetime(2026, 6, 1, tzinfo=UTC),
        finished_at=datetime(2026, 6, 1, tzinfo=UTC),
        status="finalized",
        blocking_issues=[],
        summary={},
        is_imported_legacy=is_legacy,
    )
    session.add_all([period, run])
    await session.flush()
    return period, run


def _line(run_id: uuid.UUID, employee_id: uuid.UUID, payable: str) -> PayrollLine:
    return PayrollLine(
        id=uuid.uuid4(),
        run_id=run_id,
        employee_id=employee_id,
        role="cook",
        total_payable=Decimal(payable),
    )


@pytest.mark.asyncio
async def test_legacy_run_without_payment_does_not_block_dismissal(
    async_session_factory: Any,
) -> None:
    """Легаси-заливка выплачена вне системы и записей ``PayrollPayment`` не имеет by design.

    Без фильтра ``is_imported_legacy`` такой сотрудник висел в ``dismissing`` навсегда:
    отметить выплату по импортированной ведомости API отказывается — закрыть её нечем.
    """
    async with async_session_factory() as session:
        employee = _dismissing_employee("Legacy Payroll Leaver")
        session.add(employee)
        await session.flush()
        _period, run = await _finalized_run(session, week=0, is_legacy=True)
        session.add(_line(run.id, employee.id, "4200.00"))
        await session.flush()

        state = await dismissal_reconciliation_service.settlement_state(session, employee.id)
        assert state.payroll_settled is True
        assert state.fully_settled is True

        flipped = await dismissal_reconciliation_service.reconcile_dismissing_employee(
            session, employee.id
        )
        assert flipped is True
        assert employee.status == "inactive"


@pytest.mark.asyncio
async def test_live_run_without_payment_still_blocks_dismissal(
    async_session_factory: Any,
) -> None:
    """Фильтр легаси не должен ослабить живой контур: неоплаченная ведомость держит."""
    async with async_session_factory() as session:
        employee = _dismissing_employee("Live Payroll Debtor")
        session.add(employee)
        await session.flush()
        _period, run = await _finalized_run(session, week=1, is_legacy=False)
        session.add(_line(run.id, employee.id, "5417.00"))
        await session.flush()

        state = await dismissal_reconciliation_service.settlement_state(session, employee.id)
        assert state.payroll_settled is False
        assert "невыплаченная ЗП" in state.outstanding()

        flipped = await dismissal_reconciliation_service.reconcile_dismissing_employee(
            session, employee.id
        )
        assert flipped is False
        assert employee.status == "dismissing"

        session.add(
            PayrollPayment(
                id=uuid.uuid4(),
                run_id=run.id,
                employee_id=employee.id,
                amount=Decimal("5417.00"),
                status="paid",
            )
        )
        await session.flush()
        state = await dismissal_reconciliation_service.settlement_state(session, employee.id)
        assert state.payroll_settled is True


@pytest.mark.asyncio
async def test_legacy_period_counts_as_shift_coverage(
    async_session_factory: Any,
) -> None:
    """Смена, покрытая ТОЛЬКО легаси-периодом, не считается непокрытой работой.

    Поставь фильтр ``is_imported_legacy`` и во второй пункт предиката — и вся работа за
    период заливки станет «заработанным вне ведомости»: одна вечная блокировка сменится
    другой. Ровно этот случай у сотрудника, чьи последние смены попали в майскую заливку.
    """
    async with async_session_factory() as session:
        employee = _dismissing_employee("Legacy Covered Shifts")
        session.add(employee)
        await session.flush()
        period, run = await _finalized_run(session, week=0, is_legacy=True)
        session.add(_line(run.id, employee.id, "4200.00"))
        session.add(
            ShiftLedgerEntry(
                id=uuid.uuid4(),
                work_date=period.start_date + timedelta(days=2),
                employee_id=employee.id,
                source="fallback_primary",
                opened_at=datetime(2026, 5, 21, 9, tzinfo=UTC),
            )
        )
        await session.flush()

        state = await dismissal_reconciliation_service.settlement_state(session, employee.id)
        assert state.payroll_settled is True
        assert state.fully_settled is True

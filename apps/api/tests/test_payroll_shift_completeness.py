"""Поздняя единственная роль и полная сверка табеля до расчёта/финализации."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    AttendanceEntry,
    Employee,
    EmployeePositionAssignment,
    EmployeeRoleAssignment,
    PayrollLine,
    PayrollPeriod,
    PayrollRun,
    ShiftLedgerEntry,
)
from app.services import attendance_loader, payroll_runner, shift_ledger
from app.services.payroll_shift_validation import collect_period_shift_issues

WORK_DATE = date(2026, 10, 4)
ASSIGNED_DATE = date(2026, 10, 5)
OPENED_AT = datetime(2026, 10, 4, 12, 30, tzinfo=UTC)
CLOSED_AT = datetime(2026, 10, 4, 18, 58, tzinfo=UTC)


async def make_case(session: AsyncSession, *, dual_role: bool = False):
    employee = Employee(
        id=uuid.uuid4(),
        full_name="Первый выход до назначения роли",
        iiko_id=f"late-role-{uuid.uuid4()}",
        status="active",
        category="category_1",
        hire_date=ASSIGNED_DATE,
    )
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="week",
        start_date=date(2026, 9, 29),
        end_date=ASSIGNED_DATE,
        payroll_date=date(2026, 10, 6),
        status="open",
    )
    entry = ShiftLedgerEntry(
        id=uuid.uuid4(),
        employee_id=employee.id,
        work_date=WORK_DATE,
        opened_at=OPENED_AT,
        closed_at=CLOSED_AT,
        source="fallback_primary",
        is_resolved=False,
    )
    session.add_all([employee, period, entry])
    session.add(
        EmployeePositionAssignment(
            employee_id=employee.id,
            position="Повар",
            effective_from=ASSIGNED_DATE,
        )
    )
    session.add(
        EmployeeRoleAssignment(
            employee_id=employee.id,
            payroll_role="sushi",
            category="category_1",
            effective_from=ASSIGNED_DATE,
            is_primary=True,
        )
    )
    if dual_role:
        session.add(
            EmployeeRoleAssignment(
                employee_id=employee.id,
                payroll_role="pizza",
                category="category_2",
                effective_from=ASSIGNED_DATE,
                is_primary=False,
                is_substitute=True,
            )
        )
    await session.commit()
    return employee, period, entry


def attendance_record(employee: Employee) -> dict:
    return {
        "employeeId": employee.iiko_id,
        "dateFrom": OPENED_AT.isoformat(),
        "dateTo": CLOSED_AT.isoformat(),
        "attendanceType": "Р",
    }


async def test_late_single_role_resolves_old_shift_and_enters_payroll(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shift_ledger, "ledger_today", lambda: date(2026, 10, 6))
    async with async_session_factory() as session:
        employee, period, ledger = await make_case(session)
        entries = await attendance_loader.load_attendance_entries(
            session,
            period,
            iiko_records=[attendance_record(employee)],
        )
        assert ledger.payroll_role == "sushi" and ledger.is_resolved
        assert len(entries) == 1
        assert entries[0].work_date == WORK_DATE and entries[0].minutes_worked == 388
        assert await collect_period_shift_issues(session, period, attendance_entries=entries) == []


async def test_old_incomplete_attendance_snapshot_is_reloaded_after_role_setup(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shift_ledger, "ledger_today", lambda: date(2026, 10, 6))
    async with async_session_factory() as session:
        employee, period, _ledger = await make_case(session)
        session.add(
            AttendanceEntry(
                employee_id=employee.id,
                period_id=period.id,
                work_date=ASSIGNED_DATE,
                started_at=datetime(2026, 10, 5, 9, tzinfo=UTC),
                ended_at=datetime(2026, 10, 5, 17, tzinfo=UTC),
                minutes_worked=480,
                source="iiko",
                quality_status="ok",
            )
        )
        await session.commit()

        async def fetch(*_args):
            return [attendance_record(employee)]

        monkeypatch.setattr(attendance_loader, "fetch_iiko_attendance_records", fetch)
        entries = await attendance_loader.load_attendance_entries(session, period)
        assert [entry.work_date for entry in entries] == [WORK_DATE]


async def test_two_roles_without_schedule_stay_unresolved_and_block_calculation(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shift_ledger, "ledger_today", lambda: date(2026, 10, 6))
    async with async_session_factory() as session:
        employee, period, ledger = await make_case(session, dual_role=True)
        run = await payroll_runner.run_payroll(
            session,
            period.id,
            iiko_records=[attendance_record(employee)],
        )
        assert ledger.payroll_role is None and not ledger.is_resolved
        assert run.status == "blocked"
        issue = next(item for item in run.blocking_issues if item["type"] == "unresolved_shift")
        assert issue["work_date"] == WORK_DATE.isoformat()
        assert issue["employee_id"] == str(employee.id)


async def test_manual_past_role_response_is_resolved_not_yellow(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shift_ledger, "ledger_today", lambda: date(2026, 10, 6))
    async with async_session_factory() as session:
        _employee, _period, ledger = await make_case(session)
        await shift_ledger.manually_correct(session, ledger.id, "sushi")
        response = await shift_ledger.list_ledger_for_date(session, WORK_DATE)
        row = next(item for item in response if item["id"] == str(ledger.id))
        assert row["payroll_role"] == "sushi"
        assert row["category"] == "category_1"
        assert row["is_resolved"] and row["status"] == "resolved"


async def test_ledger_read_automatically_fills_single_late_role(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shift_ledger, "ledger_today", lambda: date(2026, 10, 6))
    async with async_session_factory() as session:
        _employee, _period, ledger = await make_case(session)
        matrix = await shift_ledger.list_ledger_matrix(session, WORK_DATE)
        shifts = [
            shift
            for employee in matrix["employees"]
            for day in employee["days"]
            for shift in day["shifts"]
        ]
        shift = next(item for item in shifts if item["ledger_entry_id"] == str(ledger.id))
        assert shift["payroll_role"] == "sushi" and shift["is_resolved"]


async def test_past_manual_choice_keeps_saved_category_when_historical_roles_differ(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shift_ledger, "ledger_today", lambda: date(2026, 10, 6))
    async with async_session_factory() as session:
        employee, _period, ledger = await make_case(session)
        session.add(
            EmployeeRoleAssignment(
                employee_id=employee.id,
                payroll_role="pizza",
                category="category_2",
                effective_from=WORK_DATE,
                effective_to=ASSIGNED_DATE,
            )
        )
        await session.commit()
        await shift_ledger.manually_correct(session, ledger.id, "sushi")
        row = (await shift_ledger.list_ledger_for_date(session, WORK_DATE))[0]
        assert row["payroll_role"] == "sushi" and row["category"] == "category_1"
        assert row["is_resolved"] and row["status"] == "resolved"


async def test_finalization_checks_full_ledger_even_when_saved_blockers_are_empty(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shift_ledger, "ledger_today", lambda: date(2026, 10, 6))
    async with async_session_factory() as session:
        _employee, period, _ledger = await make_case(session, dual_role=True)
        run = PayrollRun(period_id=period.id, status="completed", blocking_issues=[], summary={})
        session.add(run)
        await session.commit()
        details = await payroll_runner.get_run(session, run.id)
        assert details["blocking_issues"][0]["type"] == "unresolved_shift"
        with pytest.raises(payroll_runner.PayrollConflictError, match="Учёт смен неполный"):
            await payroll_runner.finalize_payroll_run(session, run.id)
        assert run.status == "completed" and period.status == "open"


async def test_resolved_shift_missing_from_saved_calculation_requires_recalculation(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shift_ledger, "ledger_today", lambda: date(2026, 10, 6))
    async with async_session_factory() as session:
        employee, period, ledger = await make_case(session)
        ledger.payroll_role, ledger.category, ledger.is_resolved = "sushi", "category_1", True
        run = PayrollRun(period_id=period.id, status="completed", blocking_issues=[], summary={})
        session.add(run)
        await session.flush()
        issues = await collect_period_shift_issues(session, period, run_id=run.id)
        assert issues[0]["type"] == "missing_shift_attendance"
        session.add(
            AttendanceEntry(
                employee_id=employee.id,
                period_id=period.id,
                work_date=WORK_DATE,
                started_at=OPENED_AT,
                ended_at=CLOSED_AT,
                minutes_worked=388,
                source="iiko",
                quality_status="ok",
            )
        )
        await session.flush()
        issues = await collect_period_shift_issues(session, period, run_id=run.id)
        assert issues[0]["type"] == "stale_shift_calculation"
        session.add(
            PayrollLine(
                run_id=run.id,
                employee_id=employee.id,
                role="sushi",
                components={
                    "days": [{"date": WORK_DATE.isoformat(), "role": "sushi", "kind": "shift"}]
                },
            )
        )
        await session.flush()
        assert await collect_period_shift_issues(session, period, run_id=run.id) == []


async def test_admin_period_is_not_blocked_by_production_ledger(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        _employee, period, _ledger = await make_case(session, dual_role=True)
        period.period_type = "half_month"
        assert await collect_period_shift_issues(session, period) == []

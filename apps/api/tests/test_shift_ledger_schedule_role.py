"""Учёт смен берёт роль из ОПУБЛИКОВАННОГО графика (регресс на баг с business_date).

Раньше `load_schedule_assignments` угадывала таблицу графика по списку колонок даты
`("work_date","shift_date","date")`, а реальная колонка — `business_date`, поэтому график
никогда не читался и роль всегда падала в главную. Эти тесты гоняют РЕАЛЬНЫЙ загрузчик
против реальной таблицы `scheduled_shift` (не мокая его, в отличие от test_payroll.py).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    Employee,
    EmployeeRoleAssignment,
    ScheduledShift,
    ShiftSchedule,
)
from app.services import shift_ledger as shift_ledger_service
from app.services.shift_ledger import (
    AttendanceSnapshot,
    build_ledger_for_date,
    load_schedule_assignments,
)

WORK_DATE = date(2026, 6, 10)
OPENED_AT = datetime(2026, 6, 10, 9, 0, tzinfo=UTC)
CLOSED_AT = datetime(2026, 6, 10, 21, 0, tzinfo=UTC)


async def _make_dual_role_employee(session: AsyncSession) -> Employee:
    """Повар: главная роль Пиццерист + подменная роль Сушист (кейс Шевченко Любы)."""
    employee = Employee(
        id=uuid.uuid4(),
        full_name="Шевченко Люба",
        iiko_id=f"iiko-{uuid.uuid4()}",
    )
    session.add(employee)
    session.add(
        EmployeeRoleAssignment(
            employee_id=employee.id,
            payroll_role="pizza",
            category="category_2",
            is_primary=True,
            is_substitute=False,
            effective_from=date(2025, 1, 1),
        )
    )
    session.add(
        EmployeeRoleAssignment(
            employee_id=employee.id,
            payroll_role="sushi",
            category="category_1",
            is_primary=False,
            is_substitute=True,
            effective_from=date(2025, 1, 1),
        )
    )
    return employee


def _schedule(status: str, *, published_at: datetime | None = None) -> ShiftSchedule:
    return ShiftSchedule(
        id=uuid.uuid4(),
        date_start=WORK_DATE,
        date_end=WORK_DATE,
        status=status,
        published_at=published_at,
    )


def _shift(schedule_id: uuid.UUID, employee_id: uuid.UUID, payroll_role: str) -> ScheduledShift:
    return ScheduledShift(
        id=uuid.uuid4(),
        shift_schedule_id=schedule_id,
        business_date=WORK_DATE,
        employee_id=employee_id,
        payroll_role=payroll_role,
        planned_start_at=OPENED_AT,
        planned_end_at=CLOSED_AT,
    )


def _fake_snapshots(employee_id: uuid.UUID):
    async def _snapshots(*_args, **_kwargs):
        return [
            AttendanceSnapshot(employee_id=employee_id, opened_at=OPENED_AT, closed_at=CLOSED_AT)
        ]

    return _snapshots


async def test_load_schedule_assignments_reads_published_business_date_role(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _make_dual_role_employee(session)
        schedule = _schedule("published", published_at=datetime(2026, 6, 5, tzinfo=UTC))
        session.add(schedule)
        session.add(_shift(schedule.id, employee.id, "sushi"))
        await session.commit()

        result = await load_schedule_assignments(session, WORK_DATE, {employee.id})

    assert employee.id in result, "график по business_date должен быть распознан"
    assert result[employee.id].payroll_role == "sushi"


async def test_load_schedule_assignments_ignores_draft_and_superseded(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _make_dual_role_employee(session)
        draft = _schedule("draft")
        superseded = _schedule("superseded", published_at=datetime(2026, 6, 1, tzinfo=UTC))
        session.add_all([draft, superseded])
        session.add(_shift(draft.id, employee.id, "pizza"))
        session.add(_shift(superseded.id, employee.id, "shawarma"))
        await session.commit()

        result = await load_schedule_assignments(session, WORK_DATE, {employee.id})

    assert result == {}, "черновик и замещённый график в учёт смен не берём"


async def test_load_schedule_assignments_prefers_newest_published(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _make_dual_role_employee(session)
        older = _schedule("published", published_at=datetime(2026, 6, 2, tzinfo=UTC))
        newer = _schedule("published", published_at=datetime(2026, 6, 6, tzinfo=UTC))
        session.add_all([older, newer])
        session.add(_shift(older.id, employee.id, "pizza"))
        session.add(_shift(newer.id, employee.id, "sushi"))
        await session.commit()

        result = await load_schedule_assignments(session, WORK_DATE, {employee.id})

    assert result[employee.id].payroll_role == "sushi", "побеждает свежая published-версия"


async def test_build_ledger_uses_published_schedule_role_over_primary(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with async_session_factory() as session:
        employee = await _make_dual_role_employee(session)
        schedule = _schedule("published", published_at=datetime(2026, 6, 5, tzinfo=UTC))
        session.add(schedule)
        session.add(_shift(schedule.id, employee.id, "sushi"))
        await session.commit()

        monkeypatch.setattr(
            shift_ledger_service, "load_iiko_attendance_snapshots", _fake_snapshots(employee.id)
        )
        entries = await build_ledger_for_date(session, WORK_DATE)

    assert len(entries) == 1
    # роль подмены из графика, а НЕ главная pizza
    assert entries[0].payroll_role == "sushi"
    assert entries[0].category == "category_1"
    assert entries[0].source == "schedule"
    assert entries[0].is_resolved is True


async def test_build_ledger_falls_back_to_primary_without_published_schedule(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with async_session_factory() as session:
        employee = await _make_dual_role_employee(session)
        # только черновик с ролью sushi — использоваться не должен
        draft = _schedule("draft")
        session.add(draft)
        session.add(_shift(draft.id, employee.id, "sushi"))
        await session.commit()

        monkeypatch.setattr(
            shift_ledger_service, "load_iiko_attendance_snapshots", _fake_snapshots(employee.id)
        )
        entries = await build_ledger_for_date(session, WORK_DATE)

    assert entries[0].payroll_role == "pizza", "без опубликованного графика — главная роль"
    assert entries[0].source == "fallback_primary"

"""Ночное «окно заработанного» для авансов производственникам.

Способ 1: ночной свип (`refresh_current_week_advance_window`) заводит недельный
период ТЕКУЩЕЙ (идущей) недели вт→пн и грузит явки за отработанные дни, чтобы аванс
был доступен по факту заработанного среди недели, а не только в день выплаты (когда
стартует расчёт). Плюс правка `auto_create_next_period`: payday-кнопка запускает
пред-созданную неделю, а не «следующую после последней».
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    AttendanceEntry,
    Employee,
    EmployeePositionAssignment,
    PayrollLine,
    PayrollPeriod,
    PayrollRun,
)
from app.services import payroll_advance_availability as avail
from app.services import payroll_runner
from app.services.payroll_advance_availability import available_to_advance
from app.services.payroll_calculator import PayrollCalculationResult
from app.services.payroll_runner import (
    auto_create_next_period,
    current_week_bounds,
    ensure_weekly_period,
    refresh_current_week_advance_window,
)

# 2026-07-07 — вторник (payday), 07–13 июл — текущая неделя вт→пн, выплата 14 июл.
TUESDAY = date(2026, 7, 7)
FRIDAY = date(2026, 7, 10)
MONDAY = date(2026, 7, 13)
NEXT_TUESDAY = date(2026, 7, 14)
WEEK = (TUESDAY, MONDAY, NEXT_TUESDAY)


async def _make_cook(session: AsyncSession) -> Employee:
    employee = Employee(
        id=uuid.uuid4(),
        full_name="Повар Тест",
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
            position="Повар",
            effective_from=date(2026, 1, 1),
            effective_to=None,
        )
    )
    await session.flush()
    return employee


def _entry(employee_id: uuid.UUID, period_id: uuid.UUID, work_date: date) -> AttendanceEntry:
    return AttendanceEntry(
        id=uuid.uuid4(),
        employee_id=employee_id,
        period_id=period_id,
        work_date=work_date,
        started_at=datetime(work_date.year, work_date.month, work_date.day, 10, tzinfo=UTC),
        ended_at=datetime(work_date.year, work_date.month, work_date.day, 22, tzinfo=UTC),
        minutes_worked=720,
        source="manual",
        quality_status="ok",
    )


def test_current_week_bounds_covers_running_week() -> None:
    # Любой день недели вт→пн отображается в одну и ту же идущую неделю.
    assert current_week_bounds(TUESDAY) == WEEK  # сам вторник — начало недели
    assert current_week_bounds(FRIDAY) == WEEK  # сценарий владельца: пт → 07–13
    assert current_week_bounds(MONDAY) == WEEK  # понедельник — конец недели
    # Следующий вторник открывает новую неделю.
    assert current_week_bounds(NEXT_TUESDAY) == (
        date(2026, 7, 14),
        date(2026, 7, 20),
        date(2026, 7, 21),
    )
    # Инвариант: начало недели — всегда вторник (weekday == 1).
    for offset in range(7):
        start, end, payday = current_week_bounds(date(2026, 7, 7 + offset))
        assert start.weekday() == 1
        assert (end - start).days == 6
        assert (payday - end).days == 1


async def test_ensure_weekly_period_is_idempotent(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        first = await ensure_weekly_period(session, TUESDAY, MONDAY, NEXT_TUESDAY)
        second = await ensure_weekly_period(session, TUESDAY, MONDAY, NEXT_TUESDAY)
        await session.commit()
        assert first.id == second.id
        count = await session.scalar(
            select(func.count())
            .select_from(PayrollPeriod)
            .where(PayrollPeriod.period_type == "week", PayrollPeriod.start_date == TUESDAY)
        )
        assert count == 1


async def test_refresh_window_enables_midweek_advance(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    async with async_session_factory() as session:
        cook = await _make_cook(session)
        await session.commit()

        # До свипа: нет открытой недели → аванс недоступен (та самая дыра).
        before = await available_to_advance(session, cook, FRIDAY)
        assert before.available == Decimal("0.00")
        assert before.note is not None

        worked = (TUESDAY, date(2026, 7, 8), date(2026, 7, 9))

        async def fake_load(session_, period, *, iiko_records=None, force_reload=False):
            entries = [_entry(cook.id, period.id, day) for day in worked]
            for entry in entries:
                session_.add(entry)
            await session_.flush()
            return entries

        monkeypatch.setattr(payroll_runner, "load_attendance_entries", fake_load)

        revenue_calls: list[tuple[date, date]] = []

        async def fake_revenue(session_, date_from, date_to, *, force_refresh=False):
            revenue_calls.append((date_from, date_to))
            return {}

        monkeypatch.setattr(payroll_runner, "ensure_daily_revenue_cached", fake_revenue)

        period = await refresh_current_week_advance_window(session, today=FRIDAY)
        assert (period.start_date, period.end_date) == (TUESDAY, MONDAY)
        # Выручку кешируем по ЗАКРЫТЫМ дням: вт(07)…чт(09); пятницу (сегодня) не берём.
        assert revenue_calls == [(TUESDAY, date(2026, 7, 9))]

        async def fake_calc(_session, provisional, _run_id, entries, **_kwargs):
            line = PayrollLine(
                employee_id=cook.id, role="Повар", total_payable=Decimal("4500")
            )
            return PayrollCalculationResult(lines=[line], blocking_issues=[], summary={})

        monkeypatch.setattr(avail, "calculate_payroll_lines", fake_calc)

        # После свипа: неделя открыта, явки за отработанные дни есть → аванс доступен.
        after = await available_to_advance(session, cook, FRIDAY)
        assert after.basis == "production"
        assert after.available == Decimal("4500.00")
        assert (after.period_start, after.period_end) == (TUESDAY, FRIDAY)


async def test_refresh_window_idempotent_no_duplicate_period(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    async with async_session_factory() as session:
        async def fake_load(session_, period, *, iiko_records=None, force_reload=False):
            return []

        monkeypatch.setattr(payroll_runner, "load_attendance_entries", fake_load)

        async def fake_revenue(session_, date_from, date_to, *, force_refresh=False):
            return {}

        monkeypatch.setattr(payroll_runner, "ensure_daily_revenue_cached", fake_revenue)

        first = await refresh_current_week_advance_window(session, today=FRIDAY)
        second = await refresh_current_week_advance_window(session, today=MONDAY)
        assert first.id == second.id  # тот же период всю неделю
        count = await session.scalar(
            select(func.count())
            .select_from(PayrollPeriod)
            .where(PayrollPeriod.period_type == "week", PayrollPeriod.start_date == TUESDAY)
        )
        assert count == 1


async def test_refresh_window_caches_only_closed_days_revenue(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    async with async_session_factory() as session:
        async def fake_load(session_, period, *, iiko_records=None, force_reload=False):
            return []

        monkeypatch.setattr(payroll_runner, "load_attendance_entries", fake_load)

        calls: list[tuple[date, date]] = []

        async def fake_revenue(session_, date_from, date_to, *, force_refresh=False):
            calls.append((date_from, date_to))
            return {}

        monkeypatch.setattr(payroll_runner, "ensure_daily_revenue_cached", fake_revenue)

        # Пятница: закрыты вт–чт (07–09); сегодняшний день (пт) ещё идёт — не кешируем.
        await refresh_current_week_advance_window(session, today=FRIDAY)
        assert calls == [(TUESDAY, date(2026, 7, 9))]

        # Сам вторник (первый день недели): закрытых дней ещё нет — выручку не трогаем.
        calls.clear()
        await refresh_current_week_advance_window(session, today=TUESDAY)
        assert calls == []


async def test_auto_create_reuses_precreated_week_then_appends(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        # Пред-созданная джобом неделя без прогона.
        precreated = await ensure_weekly_period(session, TUESDAY, MONDAY, NEXT_TUESDAY)
        await session.commit()

        # Payday-кнопка должна вернуть ИМЕННО её, а не «следующую после последней».
        picked = await auto_create_next_period(session)
        assert picked.id == precreated.id

        # Как только у недели появился прогон — следующий вызов аппендит новую неделю.
        session.add(
            PayrollRun(
                id=uuid.uuid4(),
                period_id=precreated.id,
                started_at=datetime(2026, 7, 14, 9, tzinfo=UTC),
                status="running",
                blocking_issues=[],
                summary={},
            )
        )
        await session.commit()

        appended = await auto_create_next_period(session)
        assert appended.id != precreated.id
        assert appended.start_date == date(2026, 7, 14)
        assert appended.end_date == date(2026, 7, 20)

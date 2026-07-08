"""Дневной процент от выручки — самостоятельный переиспользуемый сервис.

Пул дня = выручка × тир; делится между сменами дня по весам ``coefficient × часы/12``
(floor на смену). Чистая ``distribute_day_percent`` проверяется без БД; сервис
``compute_daily_percent_for_date`` — на реальных явках + закешированной выручке.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    AppSetting,
    AttendanceEntry,
    Employee,
    EmployeeRoleAssignment,
    PayrollPeriod,
)
from app.services.daily_percent_service import compute_daily_percent_for_date
from app.services.payroll_calculator import DAILY_REVENUE_CONFIG_KEY
from app.services.payroll_percent import PercentShift, distribute_day_percent

WORK_DATE = date(2026, 7, 8)
WEEK = (date(2026, 7, 7), date(2026, 7, 13), date(2026, 7, 14))


# --- Чистая distribute_day_percent (без БД) ---------------------------------


def test_distribute_day_percent_splits_pool_by_weight() -> None:
    shifts = [
        PercentShift(employee_id="a", category="category_1", hours=12, coefficient=Decimal("10")),
        PercentShift(employee_id="b", category="category_2", hours=12, coefficient=Decimal("7.5")),
    ]
    # 140 000 → тир 4,5% → пул 6 300; веса 10 и 7,5 (сумма 17,5) → 3 600 и 2 700.
    result = distribute_day_percent(Decimal("140000"), WORK_DATE, None, shifts)
    assert result["a"] == Decimal("3600")
    assert result["b"] == Decimal("2700")


def test_distribute_day_percent_prorates_partial_shift() -> None:
    shifts = [
        PercentShift(employee_id="a", category="category_1", hours=12, coefficient=Decimal("10")),
        PercentShift(employee_id="b", category="category_1", hours=6, coefficient=Decimal("10")),
    ]
    # Полсмены — половинный вес: доли 2/3 и 1/3 пула 6 300 → 4 200 и 2 100.
    result = distribute_day_percent(Decimal("140000"), WORK_DATE, None, shifts)
    assert result["a"] == Decimal("4200")
    assert result["b"] == Decimal("2100")


def test_distribute_day_percent_zero_below_min_revenue() -> None:
    shifts = [
        PercentShift(employee_id="a", category="category_1", hours=12, coefficient=Decimal("10")),
    ]
    # Выручка ниже нижнего порога тира → пул 0 → процент 0.
    result = distribute_day_percent(Decimal("40000"), WORK_DATE, None, shifts)
    assert result["a"] == Decimal("0")


# --- Сервис compute_daily_percent_for_date (БД) -----------------------------


async def _make_cook(
    session: AsyncSession,
    *,
    payroll_role: str,
    category: str,
    name: str,
) -> Employee:
    employee = Employee(
        id=uuid.uuid4(),
        full_name=name,
        iiko_id=f"iiko-{uuid.uuid4()}",
        status="active",
        is_senior=False,
        is_deputy_senior=False,
        hire_date=None,
        fire_date=None,
        category=category,
        pin_hash="hashed-pin",
        pin_set_at=datetime(2026, 1, 1, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add(employee)
    await session.flush()
    session.add(
        EmployeeRoleAssignment(
            id=uuid.uuid4(),
            employee_id=employee.id,
            payroll_role=payroll_role,
            category=category,
            is_primary=True,
            effective_from=date(2026, 1, 1),
            effective_to=None,
        )
    )
    await session.flush()
    return employee


def _entry(
    employee_id: uuid.UUID,
    period_id: uuid.UUID,
    work_date: date,
    *,
    role: str,
    minutes: int = 720,
) -> AttendanceEntry:
    return AttendanceEntry(
        id=uuid.uuid4(),
        employee_id=employee_id,
        period_id=period_id,
        work_date=work_date,
        started_at=datetime(work_date.year, work_date.month, work_date.day, 10, tzinfo=UTC),
        ended_at=datetime(work_date.year, work_date.month, work_date.day, 22, tzinfo=UTC),
        minutes_worked=minutes,
        role=role,
        source="manual",
        quality_status="ok",
    )


async def _new_week(session: AsyncSession) -> PayrollPeriod:
    period = PayrollPeriod(
        period_type="week",
        start_date=WEEK[0],
        end_date=WEEK[1],
        payroll_date=WEEK[2],
        status="open",
    )
    session.add(period)
    await session.flush()
    return period


def _cache_revenue(session: AsyncSession, amount: str) -> None:
    session.add(
        AppSetting(
            key=DAILY_REVENUE_CONFIG_KEY,
            value={WORK_DATE.isoformat(): amount},
            value_type="object",
            category="payroll",
            display_name="Дневная выручка iiko",
            widget_type="json",
        )
    )


async def test_compute_daily_percent_splits_between_cooks(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        period = await _new_week(session)
        cook_a = await _make_cook(
            session, payroll_role="sushi", category="category_1", name="Повар А"
        )
        cook_b = await _make_cook(
            session, payroll_role="sushi", category="category_1", name="Повар Б"
        )
        session.add(_entry(cook_a.id, period.id, WORK_DATE, role="sushi"))
        session.add(_entry(cook_b.id, period.id, WORK_DATE, role="sushi"))
        _cache_revenue(session, "140000")
        await session.commit()

        result = await compute_daily_percent_for_date(session, WORK_DATE)

        assert result.daily_revenue == Decimal("140000.00")
        assert result.percent_pool > Decimal("0")
        # Одинаковая категория и часы → равные доли; сумма ≈ пул (floor ≤ 1 на смену).
        assert result.per_employee[cook_a.id] == result.per_employee[cook_b.id]
        assert result.per_employee[cook_a.id] > Decimal("0")
        distributed = result.per_employee[cook_a.id] + result.per_employee[cook_b.id]
        assert result.percent_pool - distributed <= Decimal("2")
        assert distributed <= result.percent_pool


async def test_compute_daily_percent_zero_when_revenue_not_cached(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        period = await _new_week(session)
        cook = await _make_cook(session, payroll_role="sushi", category="category_1", name="Повар")
        session.add(_entry(cook.id, period.id, WORK_DATE, role="sushi"))
        # Выручку НЕ кешируем → пул 0 → процент 0 (день ещё «не закрыт»).
        await session.commit()

        result = await compute_daily_percent_for_date(session, WORK_DATE)

        assert result.daily_revenue == Decimal("0.00")
        assert result.percent_pool == Decimal("0.00")
        assert result.per_employee[cook.id] == Decimal("0")


async def test_compute_daily_percent_empty_without_entries(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        result = await compute_daily_percent_for_date(session, WORK_DATE)
        assert result.per_employee == {}
        assert result.shifts == []

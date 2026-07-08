"""Дневной процент от выручки на сотрудника — самостоятельный переиспользуемый сервис.

Считает ту же премию-процент, что и недельный калькулятор ЗП
(``payroll_calculator.calculate_payroll_lines``), но как отдельную ДНЕВНУЮ функцию,
не привязанную к блоку расчёта аванса/ведомости. Собран из тех же общих хелперов
(роль/категория/коэффициент/веса смен, ``distribute_day_percent``), поэтому результат
согласован с ведомостью, но вызывается независимо.

Использование:
- earned-to-date аванса (косвенно — через недельный расчёт с закешированной выручкой);
- будущий модуль «Учёт смен»: в раскрытых блоках сотрудник видит заработанную за день
  премию-процент.

Выручку берём из КЕША ``payroll.daily_revenue_by_date`` (её наполняет ночной свип
``refresh_current_week_advance_window`` по закрытым дням и полный ``run_payroll``).
Если день не закеширован — выручка 0 → процент 0 (без обращения к iiko отсюда).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting, AttendanceEntry, Employee
from app.services import vacation_service
from app.services.payroll_calculator import (
    DAILY_REVENUE_CONFIG_KEY,
    EMPLOYEE_ASSIGNMENTS_CONFIG_KEY,
    SHIFT_LEDGER_CONFIG_KEY,
    category_coeff,
    category_for_payroll_entry,
    daily_revenue,
    load_employee_assignments_for_entries,
    load_shift_ledger_for_entries,
    merge_attendance_minutes,
    payroll_role_for_entry,
    percent_revenue_tiers,
)
from app.services.payroll_percent import (
    CATEGORY_COEFFICIENT_CONFIG_KEY,
    REVENUE_TIER_CONFIG_KEY,
    PercentShift,
    compute_daily_percent_pool,
    distribute_day_percent,
    load_category_coefficient_versions,
    load_revenue_tier_versions,
)

_CENTS = Decimal("0.01")


@dataclass(slots=True)
class DailyPercentShift:
    """Одна смена дня: чей вес и сколько процента ей досталось из дневного пула."""

    employee_id: uuid.UUID
    role: str
    station: str | None
    category: str
    hours: Decimal
    percent: Decimal


@dataclass(slots=True)
class DailyPercentResult:
    work_date: date
    daily_revenue: Decimal
    percent_pool: Decimal
    per_employee: dict[uuid.UUID, Decimal]
    shifts: list[DailyPercentShift]


def _empty_result(work_date: date) -> DailyPercentResult:
    return DailyPercentResult(
        work_date=work_date,
        daily_revenue=Decimal("0.00"),
        percent_pool=Decimal("0.00"),
        per_employee={},
        shifts=[],
    )


async def _load_percent_settings(
    session: AsyncSession,
    work_date: date,
    entries: list[AttendanceEntry],
) -> dict:
    """Минимальный набор настроек, нужный именно для дневного процента."""
    revenue_setting = await session.scalar(
        select(AppSetting).where(AppSetting.key == DAILY_REVENUE_CONFIG_KEY)
    )
    revenue_cache = (
        revenue_setting.value
        if revenue_setting is not None and isinstance(revenue_setting.value, Mapping)
        else {}
    )
    return {
        DAILY_REVENUE_CONFIG_KEY: revenue_cache,
        REVENUE_TIER_CONFIG_KEY: await load_revenue_tier_versions(session, work_date, work_date),
        CATEGORY_COEFFICIENT_CONFIG_KEY: await load_category_coefficient_versions(
            session, work_date, work_date
        ),
        EMPLOYEE_ASSIGNMENTS_CONFIG_KEY: await load_employee_assignments_for_entries(
            session, entries
        ),
        SHIFT_LEDGER_CONFIG_KEY: await load_shift_ledger_for_entries(session, entries),
    }


async def compute_daily_percent_for_date(
    session: AsyncSession,
    work_date: date,
) -> DailyPercentResult:
    """Процент-премия за один день по каждому сотруднику (и по каждой смене дня).

    Пул = выручка_дня × тир; делится между всеми участниками дня по весам
    ``coefficient × min(hours, 12ч) / 12ч`` c floor на смену (см. ``distribute_day_percent``).
    Знаменатель — сумма весов ВСЕХ смен дня, поэтому загружаем все явки дня, а не одного
    сотрудника. Отпускные дни в проценте не участвуют — как и в ведомости.
    """
    entries = list(
        (
            await session.scalars(
                select(AttendanceEntry).where(AttendanceEntry.work_date == work_date)
            )
        ).all()
    )
    if not entries:
        return _empty_result(work_date)

    employee_ids = {entry.employee_id for entry in entries}
    employees = {
        employee.id: employee
        for employee in (
            await session.scalars(select(Employee).where(Employee.id.in_(employee_ids)))
        ).all()
    }
    vacation_days = await vacation_service.vacation_days_for_payroll_period(
        session,
        period_start=work_date,
        period_end=work_date,
    )
    settings = await _load_percent_settings(session, work_date, entries)

    # Группируем как ведомость: (сотрудник, payroll-роль, станция). Пересекающиеся явки
    # одного дня объединяем в минуты, чтобы дубли iiko не задваивали часы.
    group_intervals: dict[tuple[uuid.UUID, str, str | None], list] = defaultdict(list)
    group_category: dict[tuple[uuid.UUID, str, str | None], str] = {}
    for entry in entries:
        if (entry.employee_id, entry.work_date) in vacation_days:
            continue
        employee = employees.get(entry.employee_id)
        if employee is None:
            continue
        role = payroll_role_for_entry(entry, employee, settings)
        station = (entry.station or "").strip() or None
        key = (entry.employee_id, role, station)
        group_intervals[key].append((entry.started_at, entry.ended_at, entry.minutes_worked))
        group_category[key] = category_for_payroll_entry(
            settings, employee, work_date, role, station
        )

    shifts: list[PercentShift] = []
    for key, intervals in group_intervals.items():
        category = group_category[key]
        minutes = merge_attendance_minutes(intervals)
        shifts.append(
            PercentShift(
                employee_id=key,
                category=category,
                hours=Decimal(minutes) / Decimal(60),
                coefficient=category_coeff(settings, category, work_date),
            )
        )

    revenue = daily_revenue(settings, work_date)
    tiers = percent_revenue_tiers(settings)
    distribution = distribute_day_percent(revenue, work_date, tiers, shifts)

    per_employee: dict[uuid.UUID, Decimal] = defaultdict(lambda: Decimal("0"))
    breakdown: list[DailyPercentShift] = []
    for shift in shifts:
        employee_id, role, station = shift.employee_id
        percent = distribution.get(shift.employee_id, Decimal("0"))
        per_employee[employee_id] += percent
        breakdown.append(
            DailyPercentShift(
                employee_id=employee_id,
                role=role,
                station=station,
                category=shift.category,
                hours=Decimal(str(shift.hours)),
                percent=percent,
            )
        )

    return DailyPercentResult(
        work_date=work_date,
        daily_revenue=revenue.quantize(_CENTS),
        percent_pool=compute_daily_percent_pool(revenue, work_date, tiers).quantize(_CENTS),
        per_employee={employee_id: amount for employee_id, amount in per_employee.items()},
        shifts=breakdown,
    )

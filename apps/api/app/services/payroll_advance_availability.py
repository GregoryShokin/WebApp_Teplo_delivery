"""Доступно к авансу — earned-to-date по сотруднику на дату.

Аванс (право A) ограничен заработанным на дату. Считаем единообразно по принципу
«сколько сотрудник заработал бы, если бы текущий открытый период закончился сегодня»:

- Окладник (админ-должности): оклад × прошло_дней / дней_в_полупериоде — чистая
  формула без данных (см. `okladnik_earned_to_date`).
- Мойщица: смены ≤ as_of × ставку-из-пула.
- Производственный (повар/кассир): провизорный прогон реального недельного
  калькулятора с `period.end = as_of` по УЖЕ загруженным явкам ≤ as_of (решение:
  «из загруженных явок», без live-iiko в момент выдачи). `total_payable` — netto.

Доступно к авансу = earned-to-date − уже выданные авансы за текущий период.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AttendanceEntry, Employee, PayrollPeriod, SalaryAdvance
from app.services.employee_effective_events import get_position_on_date
from app.services.payroll_admin import (
    DISHWASHER_POSITIONS,
    OKLADNIK_POSITIONS,
    HALF_MONTH_SPLIT_DAY,
    _dishwasher_shift_rate,
    _first_half,
    _load_dishwasher_pool,
    _load_dishwasher_shift_counts,
    _load_okladnik_payout_modes,
    _okladnik_payout_mode,
    _second_half,
    load_admin_oklad,
    okladnik_earned_to_date,
)
from app.services.payroll_calculator import calculate_payroll_lines, decimal

_CENTS = Decimal("0.01")


@dataclass(slots=True)
class AdvanceAvailability:
    employee_id: uuid.UUID
    as_of: date
    period_start: date | None
    period_end: date | None
    basis: str  # okladnik | dishwasher | production | none
    earned_to_date: Decimal
    already_advanced: Decimal
    available: Decimal
    note: str | None = None


def _half_month_bounds(as_of: date) -> tuple[date, date]:
    """Границы полумесячного периода, содержащего `as_of`."""
    if as_of.day <= HALF_MONTH_SPLIT_DAY:
        start, end, _ = _first_half(as_of.year, as_of.month)
    else:
        start, end, _ = _second_half(as_of.year, as_of.month)
    return start, end


async def _open_weekly_period(session: AsyncSession, as_of: date) -> PayrollPeriod | None:
    return await session.scalar(
        select(PayrollPeriod)
        .where(
            PayrollPeriod.period_type == "week",
            PayrollPeriod.start_date <= as_of,
            PayrollPeriod.end_date >= as_of,
        )
        .order_by(PayrollPeriod.start_date.desc())
        .limit(1)
    )


async def _okladnik_earned(
    session: AsyncSession,
    employee: Employee,
    position: str,
    as_of: date,
) -> tuple[Decimal, date, date]:
    start, end = _half_month_bounds(as_of)
    period = PayrollPeriod(
        period_type="half_month",
        start_date=start,
        end_date=end,
        payroll_date=end,
        status="open",
    )
    oklad = await load_admin_oklad(session, employee.id, position, end)
    if oklad is None:
        return Decimal("0.00"), start, end
    modes = await _load_okladnik_payout_modes(session)
    mode = _okladnik_payout_mode(modes, position)
    earned = okladnik_earned_to_date(oklad, mode, employee, period, as_of)
    return earned, start, end


async def _dishwasher_earned(
    session: AsyncSession,
    employee: Employee,
    as_of: date,
) -> tuple[Decimal, date, date]:
    start, end = _half_month_bounds(as_of)
    period = PayrollPeriod(
        period_type="half_month",
        start_date=start,
        end_date=end,
        payroll_date=end,
        status="open",
    )
    pool = await _load_dishwasher_pool(session)
    rate = _dishwasher_shift_rate(pool, period)
    # Смены только по прошедшие дни (≤ as_of) текущего полупериода.
    counts = await _load_dishwasher_shift_counts(session, [employee.id], start, as_of)
    shifts = counts.get(employee.id, 0)
    earned = (rate * Decimal(shifts)).quantize(_CENTS)
    return earned, start, end


async def _production_earned(
    session: AsyncSession,
    employee: Employee,
    as_of: date,
) -> tuple[Decimal, date | None, date | None, str | None]:
    period = await _open_weekly_period(session, as_of)
    if period is None:
        return Decimal("0.00"), None, None, "Нет открытого недельного периода"
    # Только уже загруженные явки ≤ as_of (без live-iiko). Процентный пул считается
    # по всем сотрудникам этих дней, поэтому грузим явки периода целиком, усекая по дате.
    entries = list(
        (
            await session.scalars(
                select(AttendanceEntry).where(
                    AttendanceEntry.period_id == period.id,
                    AttendanceEntry.work_date <= as_of,
                )
            )
        ).all()
    )
    if not entries:
        return Decimal("0.00"), period.start_date, as_of, "Явки за период ещё не загружены"
    provisional = PayrollPeriod(
        period_type="week",
        start_date=period.start_date,
        end_date=as_of,
        payroll_date=period.payroll_date,
        status="open",
    )
    result = await calculate_payroll_lines(session, provisional, uuid.uuid4(), entries)
    if result.blocking_issues:
        return Decimal("0.00"), period.start_date, as_of, "Расчёт заблокирован — проверьте явки/ставки"
    earned = sum(
        (decimal(line.total_payable) for line in result.lines if line.employee_id == employee.id),
        Decimal("0"),
    )
    return earned.quantize(_CENTS), period.start_date, as_of, None


async def _already_advanced_in_period(
    session: AsyncSession,
    employee_id: uuid.UUID,
    period_start: date,
    period_end: date,
) -> Decimal:
    """Сумма выданных за период авансов (не отменённых) — уменьшает доступное."""
    rows = (
        await session.scalars(
            select(SalaryAdvance).where(
                SalaryAdvance.employee_id == employee_id,
                SalaryAdvance.issued_on >= period_start,
                SalaryAdvance.issued_on <= period_end,
                SalaryAdvance.status != "cancelled",
            )
        )
    ).all()
    return sum((decimal(row.amount) for row in rows), Decimal("0")).quantize(_CENTS)


async def available_to_advance(
    session: AsyncSession,
    employee: Employee,
    as_of: date,
) -> AdvanceAvailability:
    """Доступно к авансу = earned-to-date − уже выданные авансы за текущий период."""
    position = await get_position_on_date(session, employee.id, as_of)
    position = position or employee.position or ""

    note: str | None = None
    if position in OKLADNIK_POSITIONS:
        basis = "okladnik"
        earned, start, end = await _okladnik_earned(session, employee, position, as_of)
    elif position in DISHWASHER_POSITIONS:
        basis = "dishwasher"
        earned, start, end = await _dishwasher_earned(session, employee, as_of)
    else:
        basis = "production"
        earned, start, end, note = await _production_earned(session, employee, as_of)

    if start is None or end is None:
        return AdvanceAvailability(
            employee_id=employee.id,
            as_of=as_of,
            period_start=None,
            period_end=None,
            basis="none",
            earned_to_date=Decimal("0.00"),
            already_advanced=Decimal("0.00"),
            available=Decimal("0.00"),
            note=note,
        )

    already = await _already_advanced_in_period(session, employee.id, start, end)
    available = max(earned - already, Decimal("0")).quantize(_CENTS)
    return AdvanceAvailability(
        employee_id=employee.id,
        as_of=as_of,
        period_start=start,
        period_end=end,
        basis=basis,
        earned_to_date=earned.quantize(_CENTS),
        already_advanced=already,
        available=available,
        note=note,
    )

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import PayrollAdjustment, PayrollPeriod, PayrollRun


class PayrollAdjustmentLockedError(ValueError):
    pass


async def assert_date_not_locked(session: AsyncSession, work_date: date) -> None:
    """Проверить, что нет finalized payroll_run, чей период покрывает work_date."""
    if await is_date_locked(session, work_date):
        raise PayrollAdjustmentLockedError("Период зафиксирован, изменения невозможны")


async def is_date_locked(session: AsyncSession, work_date: date) -> bool:
    locked_period_id = await session.scalar(
        select(PayrollPeriod.id)
        .select_from(PayrollRun)
        .join(PayrollPeriod, PayrollPeriod.id == PayrollRun.period_id)
        .where(
            PayrollRun.status == "finalized",
            PayrollPeriod.start_date <= work_date,
            PayrollPeriod.end_date >= work_date,
        )
        .limit(1)
    )
    return locked_period_id is not None


async def load_locked_dates_for_period(
    session: AsyncSession,
    *,
    period_start: date,
    period_end: date,
) -> set[date]:
    result = await session.execute(
        select(PayrollPeriod.start_date, PayrollPeriod.end_date)
        .select_from(PayrollRun)
        .join(PayrollPeriod, PayrollPeriod.id == PayrollRun.period_id)
        .where(
            PayrollRun.status == "finalized",
            PayrollPeriod.start_date <= period_end,
            PayrollPeriod.end_date >= period_start,
        )
    )
    locked_dates: set[date] = set()
    for start_date, end_date in result.all():
        current = max(start_date, period_start)
        last = min(end_date, period_end)
        while current <= last:
            locked_dates.add(current)
            current = date.fromordinal(current.toordinal() + 1)
    return locked_dates


async def load_adjustments_for_period(
    session: AsyncSession,
    *,
    employee_ids: Iterable[uuid.UUID],
    period_start: date,
    period_end: date,
) -> dict[tuple[uuid.UUID, date], list[PayrollAdjustment]]:
    employee_ids = set(employee_ids)
    if not employee_ids:
        return {}
    result = await session.scalars(
        select(PayrollAdjustment)
        .options(selectinload(PayrollAdjustment.category))
        .where(
            PayrollAdjustment.employee_id.in_(employee_ids),
            PayrollAdjustment.work_date >= period_start,
            PayrollAdjustment.work_date <= period_end,
        )
        .order_by(PayrollAdjustment.work_date, PayrollAdjustment.created_at)
    )
    adjustments: dict[tuple[uuid.UUID, date], list[PayrollAdjustment]] = defaultdict(list)
    for adjustment in result.all():
        adjustments[(adjustment.employee_id, adjustment.work_date)].append(adjustment)
    return dict(adjustments)

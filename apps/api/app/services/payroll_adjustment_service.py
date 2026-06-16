from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import PayrollAdjustment, PayrollAdjustmentCategory, PayrollPeriod, PayrollRun

# Должности админского каденса (полумесячная ведомость 1/15) читаются из реестра
# должностей: archetype okladnik/shift_pool → position_registry.admin_payroll_positions().

LEGACY_CATEGORIES = [
    {"code": "premium", "type": "bonus", "display_name": "Премия", "sort_order": 10},
    {
        "code": "audit_penalty",
        "type": "penalty",
        "display_name": "Штраф по ревизии",
        "sort_order": 20,
    },
    {
        "code": "manual_penalty",
        "type": "penalty",
        "display_name": "Удержание/штраф",
        "sort_order": 30,
    },
]


class PayrollAdjustmentLockedError(ValueError):
    pass


async def ensure_legacy_categories(session: AsyncSession) -> None:
    for spec in LEGACY_CATEGORIES:
        existing = await session.scalar(
            select(PayrollAdjustmentCategory).where(PayrollAdjustmentCategory.code == spec["code"])
        )
        if existing is None:
            session.add(
                PayrollAdjustmentCategory(
                    id=uuid.uuid4(),
                    code=spec["code"],
                    type=spec["type"],
                    display_name=spec["display_name"],
                    sort_order=spec["sort_order"],
                    is_active=True,
                )
            )
    await session.flush()


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


async def is_production_date_locked(session: AsyncSession, work_date: date) -> bool:
    """Дата закрыта для ПРОИЗВОДСТВЕННОЙ (недельной) ведомости.

    Учитывает только финализированные недельные периоды (``period_type='week'``);
    полумесячная админская финализация её НЕ блокирует (симметрично
    ``_is_admin_date_locked``). Штрафы ревизий идут поварам/кассирам в недельную
    ведомость — для них релевантна именно эта блокировка, а не общая ``is_date_locked``.
    """
    locked_period_id = await session.scalar(
        select(PayrollPeriod.id)
        .select_from(PayrollRun)
        .join(PayrollPeriod, PayrollPeriod.id == PayrollRun.period_id)
        .where(
            PayrollRun.status == "finalized",
            PayrollPeriod.period_type == "week",
            PayrollPeriod.start_date <= work_date,
            PayrollPeriod.end_date >= work_date,
        )
        .limit(1)
    )
    return locked_period_id is not None


async def assert_production_date_not_locked(session: AsyncSession, work_date: date) -> None:
    """Как :func:`assert_date_not_locked`, но только по недельной (производственной)
    финализации — админская полумесячная ведомость не блокирует штрафы ревизий."""
    if await is_production_date_locked(session, work_date):
        raise PayrollAdjustmentLockedError("Период зафиксирован, изменения невозможны")


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
    roles: Iterable[str] | None = None,
    exclude_roles: Iterable[str] | None = None,
) -> dict[tuple[uuid.UUID, date], list[PayrollAdjustment]]:
    """Загрузить премии/штрафы за период, сгруппированные по (сотрудник, дата).

    ``roles`` — взять только указанные роли (нужно админской ведомости).
    ``exclude_roles`` — исключить указанные роли (производственная ведомость
    исключает админские роли, чтобы не задвоить штрафы сотрудников с двумя ролями).
    """
    employee_ids = set(employee_ids)
    if not employee_ids:
        return {}
    conditions = [
        PayrollAdjustment.employee_id.in_(employee_ids),
        PayrollAdjustment.work_date >= period_start,
        PayrollAdjustment.work_date <= period_end,
    ]
    if roles is not None:
        conditions.append(PayrollAdjustment.role.in_(set(roles)))
    if exclude_roles is not None:
        conditions.append(PayrollAdjustment.role.notin_(set(exclude_roles)))
    result = await session.scalars(
        select(PayrollAdjustment)
        .options(selectinload(PayrollAdjustment.category))
        .where(*conditions)
        .order_by(PayrollAdjustment.work_date, PayrollAdjustment.created_at)
    )
    adjustments: dict[tuple[uuid.UUID, date], list[PayrollAdjustment]] = defaultdict(list)
    for adjustment in result.all():
        adjustments[(adjustment.employee_id, adjustment.work_date)].append(adjustment)
    return dict(adjustments)

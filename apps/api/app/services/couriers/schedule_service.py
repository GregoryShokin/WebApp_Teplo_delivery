from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CourierScheduleEntry
from app.services.couriers.common import get_courier_or_404, get_employee_or_404
from app.services.couriers.shift_matching import recalculate_matches


@dataclass(frozen=True)
class ScheduleFilters:
    date_from: date
    date_to: date
    courier_id: uuid.UUID | None = None


async def upsert_entry(
    session: AsyncSession,
    courier_id: uuid.UUID,
    work_date: date,
    planned_start_at: datetime,
    planned_end_at: datetime,
    comment: str | None,
    actor_id: uuid.UUID,
) -> CourierScheduleEntry:
    await get_courier_or_404(session, courier_id)
    await get_employee_or_404(session, actor_id)
    if planned_end_at <= planned_start_at:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="planned_end_at must be after planned_start_at",
        )

    entry = await get_entry(session, courier_id, work_date)
    if entry is None:
        entry = CourierScheduleEntry(
            courier_employee_id=courier_id,
            work_date=work_date,
            planned_start_at=planned_start_at,
            planned_end_at=planned_end_at,
            comment=comment,
            created_by=actor_id,
        )
        session.add(entry)
    else:
        entry.planned_start_at = planned_start_at
        entry.planned_end_at = planned_end_at
        entry.comment = comment
    await session.flush()
    await recalculate_matches(session, work_date, work_date, employee_ids=[courier_id])
    return entry


async def get_entry(
    session: AsyncSession,
    courier_id: uuid.UUID,
    work_date: date,
) -> CourierScheduleEntry | None:
    return await session.scalar(
        select(CourierScheduleEntry).where(
            CourierScheduleEntry.courier_employee_id == courier_id,
            CourierScheduleEntry.work_date == work_date,
        )
    )


async def list_entries(
    session: AsyncSession,
    filters: ScheduleFilters,
) -> list[CourierScheduleEntry]:
    stmt = select(CourierScheduleEntry).where(
        CourierScheduleEntry.work_date >= filters.date_from,
        CourierScheduleEntry.work_date <= filters.date_to,
    )
    if filters.courier_id is not None:
        stmt = stmt.where(CourierScheduleEntry.courier_employee_id == filters.courier_id)
    result = await session.scalars(
        stmt.order_by(CourierScheduleEntry.work_date, CourierScheduleEntry.planned_start_at)
    )
    return list(result.all())


def entry_payload(entry: CourierScheduleEntry) -> dict:
    return {
        "id": entry.id,
        "courier_employee_id": entry.courier_employee_id,
        "work_date": entry.work_date,
        "planned_start_at": entry.planned_start_at,
        "planned_end_at": entry.planned_end_at,
        "comment": entry.comment,
        "created_by": entry.created_by,
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
    }

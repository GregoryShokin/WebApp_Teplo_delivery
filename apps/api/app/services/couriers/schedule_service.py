from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal, cast
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AppSetting,
    CourierIikoShift,
    CourierScheduleEntry,
    CourierShiftMatch,
    Employee,
)
from app.services.couriers.common import get_courier_or_404, get_employee_or_404
from app.services.couriers.shift_matching import (
    clear_matches_for_deleted_schedule_entry,
    recalculate_matches,
)

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
ScheduleCategory = Literal["primary", "secondary"]
WorkStatus = Literal["working", "reserve"]
DEFAULT_SHIFT_START = time(10, 0)
DEFAULT_SHIFT_END = time(22, 0)
DEFAULT_SHIFT_START_SETTING = "couriers.schedule.default_start_time"
DEFAULT_SHIFT_END_SETTING = "couriers.schedule.default_end_time"


@dataclass(frozen=True)
class ScheduleFilters:
    date_from: date
    date_to: date
    courier_id: uuid.UUID | None = None
    courier_ids: tuple[uuid.UUID, ...] = ()


async def upsert_entry(
    session: AsyncSession,
    courier_id: uuid.UUID,
    work_date: date,
    category: ScheduleCategory,
    planned_start_at: datetime | None,
    planned_end_at: datetime | None,
    comment: str | None,
    actor_id: uuid.UUID,
) -> CourierScheduleEntry:
    await get_courier_or_404(session, courier_id)
    await get_employee_or_404(session, actor_id)
    if category not in {"primary", "secondary"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="category must be primary or secondary",
        )

    resolved_start_at, resolved_end_at = await resolve_shift_window(
        session,
        work_date,
        planned_start_at,
        planned_end_at,
    )
    if resolved_end_at <= resolved_start_at:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="planned_end_at must be after planned_start_at",
        )

    entry = await get_entry(session, courier_id, work_date)
    if entry is None:
        entry = CourierScheduleEntry(
            courier_employee_id=courier_id,
            work_date=work_date,
            category=category,
            planned_start_at=resolved_start_at,
            planned_end_at=resolved_end_at,
            comment=comment,
            created_by=actor_id,
        )
        session.add(entry)
    else:
        entry.category = category
        entry.planned_start_at = resolved_start_at
        entry.planned_end_at = resolved_end_at
        entry.comment = comment
    await session.flush()
    await recalculate_matches(session, work_date, work_date, employee_ids=[courier_id])
    return entry


async def delete_entry(
    session: AsyncSession,
    courier_id: uuid.UUID,
    work_date: date,
    actor_id: uuid.UUID,
) -> None:
    await get_courier_or_404(session, courier_id)
    await get_employee_or_404(session, actor_id)
    entry = await get_entry(session, courier_id, work_date)
    if entry is None:
        return
    if entry.id is not None:
        await clear_matches_for_deleted_schedule_entry(session, entry.id)
    await session.delete(entry)
    await session.flush()
    await recalculate_matches(session, work_date, work_date, employee_ids=[courier_id])


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
    if filters.courier_ids:
        stmt = stmt.where(CourierScheduleEntry.courier_employee_id.in_(filters.courier_ids))
    result = await session.scalars(
        stmt.order_by(
            CourierScheduleEntry.courier_employee_id,
            CourierScheduleEntry.work_date,
            CourierScheduleEntry.planned_start_at,
        )
    )
    return list(result.all())


async def get_matrix(
    session: AsyncSession,
    from_date: date,
    to_date: date,
    courier_ids: list[uuid.UUID] | None = None,
) -> list[CourierScheduleEntry]:
    return await list_entries(
        session,
        ScheduleFilters(
            date_from=from_date,
            date_to=to_date,
            courier_ids=tuple(courier_ids or ()),
        ),
    )


async def get_work_statuses(
    session: AsyncSession,
    employee_ids: list[uuid.UUID],
    as_of: datetime | None = None,
) -> dict[uuid.UUID, WorkStatus]:
    """Return working if a courier has any iiko shift in the last 14 days, else reserve."""
    if not employee_ids:
        return {}

    cutoff = normalize_shift_datetime(as_of or datetime.now(MOSCOW_TZ)) - timedelta(days=14)
    has_recent_shift = (
        select(CourierIikoShift.id)
        .where(
            CourierIikoShift.employee_id == Employee.id,
            CourierIikoShift.opened_at >= cutoff,
        )
        .exists()
    )
    stmt = select(
        Employee.id,
        case((has_recent_shift, "working"), else_="reserve").label("work_status"),
    ).where(Employee.id.in_(employee_ids))

    statuses: dict[uuid.UUID, WorkStatus] = {employee_id: "reserve" for employee_id in employee_ids}
    for employee_id, work_status in (await session.execute(stmt)).all():
        statuses[employee_id] = cast(WorkStatus, work_status)
    return statuses


async def list_matched_entries(
    session: AsyncSession,
    filters: ScheduleFilters,
) -> list[dict]:
    entries = await list_entries(session, filters)
    matches = await list_matches(session, filters)
    shifts_by_id = await load_iiko_shifts_for_matches(session, matches)
    courier_ids = {entry.courier_employee_id for entry in entries} | {
        match.courier_employee_id for match in matches
    }
    work_statuses = await get_work_statuses(
        session,
        list(courier_ids),
    )
    matches_by_schedule_id = {
        match.schedule_entry_id: match for match in matches if match.schedule_entry_id is not None
    }
    payloads = [
        matched_entry_payload(
            entry,
            matches_by_schedule_id.get(entry.id),
            shifts_by_id,
        )
        for entry in entries
    ]

    entry_ids = {entry.id for entry in entries}
    for match in matches:
        if match.schedule_entry_id in entry_ids:
            continue
        if match.schedule_entry_id is None:
            payloads.append(match_only_payload(match, shifts_by_id))

    for payload in payloads:
        payload["work_status"] = work_statuses.get(payload["courier_employee_id"])

    payloads.sort(key=lambda row: (str(row["courier_employee_id"]), row["work_date"]))
    return payloads


async def list_matches(
    session: AsyncSession,
    filters: ScheduleFilters,
) -> list[CourierShiftMatch]:
    stmt = select(CourierShiftMatch).where(
        CourierShiftMatch.work_date >= filters.date_from,
        CourierShiftMatch.work_date <= filters.date_to,
    )
    if filters.courier_id is not None:
        stmt = stmt.where(CourierShiftMatch.courier_employee_id == filters.courier_id)
    if filters.courier_ids:
        stmt = stmt.where(CourierShiftMatch.courier_employee_id.in_(filters.courier_ids))
    result = await session.scalars(
        stmt.order_by(CourierShiftMatch.courier_employee_id, CourierShiftMatch.work_date)
    )
    return list(result.all())


async def load_iiko_shifts_for_matches(
    session: AsyncSession,
    matches: list[CourierShiftMatch],
) -> dict[int, CourierIikoShift]:
    shift_ids = {match.iiko_shift_id for match in matches if match.iiko_shift_id is not None}
    if not shift_ids:
        return {}
    result = await session.scalars(
        select(CourierIikoShift).where(CourierIikoShift.id.in_(shift_ids))
    )
    return {shift.id: shift for shift in result.all()}


def entry_payload(entry: CourierScheduleEntry) -> dict:
    return {
        "id": entry.id,
        "courier_employee_id": entry.courier_employee_id,
        "work_date": entry.work_date,
        "category": entry.category,
        "planned_start_at": entry.planned_start_at,
        "planned_end_at": entry.planned_end_at,
        "comment": entry.comment,
        "created_by": entry.created_by,
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
    }


def matched_entry_payload(
    entry: CourierScheduleEntry,
    match: CourierShiftMatch | None,
    shifts_by_id: dict[int, CourierIikoShift],
) -> dict:
    payload = entry_payload(entry)
    payload.update(match_payload(match, shifts_by_id))
    if should_mark_not_started(entry, match):
        payload["status"] = "not_started"
        payload["late_minutes"] = None
        payload["worked_minutes"] = None
        payload["deliveries_count"] = None
        payload["iiko_shift_id"] = None
        payload["opened_at"] = None
        payload["closed_at"] = None
    return payload


def match_only_payload(
    match: CourierShiftMatch,
    shifts_by_id: dict[int, CourierIikoShift],
) -> dict:
    payload = {
        "id": None,
        "courier_employee_id": match.courier_employee_id,
        "work_date": match.work_date,
        "category": None,
        "planned_start_at": None,
        "planned_end_at": None,
        "comment": None,
    }
    payload.update(match_payload(match, shifts_by_id))
    return payload


def match_payload(
    match: CourierShiftMatch | None,
    shifts_by_id: dict[int, CourierIikoShift],
) -> dict:
    if match is None:
        return {
            "status": None,
            "late_minutes": None,
            "worked_minutes": None,
            "deliveries_count": None,
            "iiko_shift_id": None,
            "opened_at": None,
            "closed_at": None,
        }
    shift = shifts_by_id.get(match.iiko_shift_id) if match.iiko_shift_id is not None else None
    return {
        "status": _status_value(match.status),
        "late_minutes": match.late_minutes,
        "worked_minutes": match.worked_minutes,
        "deliveries_count": match.deliveries_count,
        "iiko_shift_id": match.iiko_shift_id,
        "opened_at": shift.opened_at if shift is not None else None,
        "closed_at": shift.closed_at if shift is not None else None,
    }


def should_mark_not_started(entry: CourierScheduleEntry, match: CourierShiftMatch | None) -> bool:
    today = datetime.now(MOSCOW_TZ).date()
    if entry.work_date < today:
        return False
    if match is None:
        return True
    return match.iiko_shift_id is None and _status_value(match.status) in {
        "no_show_primary",
        "no_show_secondary",
    }


async def resolve_shift_window(
    session: AsyncSession,
    work_date: date,
    planned_start_at: datetime | None,
    planned_end_at: datetime | None,
) -> tuple[datetime, datetime]:
    default_start, default_end = await default_shift_times(session)
    start_at = normalize_shift_datetime(
        planned_start_at or datetime.combine(work_date, default_start, tzinfo=MOSCOW_TZ)
    )
    end_at = normalize_shift_datetime(
        planned_end_at or datetime.combine(work_date, default_end, tzinfo=MOSCOW_TZ)
    )
    if planned_end_at is None and end_at <= start_at:
        end_at += timedelta(days=1)
    return start_at, end_at


async def default_shift_times(session: AsyncSession) -> tuple[time, time]:
    result = await session.scalars(
        select(AppSetting).where(
            AppSetting.key.in_([DEFAULT_SHIFT_START_SETTING, DEFAULT_SHIFT_END_SETTING])
        )
    )
    values = {setting.key: setting.value for setting in result.all()}
    return (
        parse_time_setting(values.get(DEFAULT_SHIFT_START_SETTING), DEFAULT_SHIFT_START),
        parse_time_setting(values.get(DEFAULT_SHIFT_END_SETTING), DEFAULT_SHIFT_END),
    )


def normalize_shift_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=MOSCOW_TZ)
    return value


def parse_time_setting(value: object, fallback: time) -> time:
    if not isinstance(value, str):
        return fallback
    try:
        hours, minutes = value.split(":", 1)
        return time(int(hours), int(minutes))
    except (TypeError, ValueError):
        return fallback


def _status_value(value: object) -> str | None:
    if value is None:
        return None
    return getattr(value, "value", value)

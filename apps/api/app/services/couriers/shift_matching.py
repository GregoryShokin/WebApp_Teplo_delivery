from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CourierIikoShift,
    CourierScheduleEntry,
    CourierShiftMatch,
    CourierShiftMatchStatus,
    DeliveryOrder,
    Employee,
)

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
MIN_WORKED_MINUTES = 120
NON_DELIVERY_STATUSES = {
    "cancelled",
    "canceled",
    "deleted",
    "orderdeleted",
    "storno",
    "void",
    "отменен",
    "отменён",
    "удален",
    "удалён",
}


@dataclass(slots=True)
class MatchRecalculationReport:
    matched_primary: int = 0
    short_primary: int = 0
    no_show_primary: int = 0
    matched_secondary: int = 0
    short_secondary: int = 0
    no_show_secondary: int = 0
    helping: int = 0
    not_counted: int = 0
    inserted: int = 0
    updated: int = 0
    deleted: int = 0

    @property
    def total(self) -> int:
        return (
            self.matched_primary
            + self.short_primary
            + self.no_show_primary
            + self.matched_secondary
            + self.short_secondary
            + self.no_show_secondary
            + self.helping
            + self.not_counted
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "matched_primary": self.matched_primary,
            "short_primary": self.short_primary,
            "no_show_primary": self.no_show_primary,
            "matched_secondary": self.matched_secondary,
            "short_secondary": self.short_secondary,
            "no_show_secondary": self.no_show_secondary,
            "helping": self.helping,
            "not_counted": self.not_counted,
            "inserted": self.inserted,
            "updated": self.updated,
            "deleted": self.deleted,
        }


def build_shift_matches(
    *,
    schedules: Sequence[CourierScheduleEntry],
    shifts: Sequence[CourierIikoShift],
    delivery_counts: dict[tuple[uuid.UUID, date], int],
    from_date: date,
    to_date: date,
    employee_ids: Iterable[uuid.UUID] | None = None,
    now: datetime | None = None,
) -> list[CourierShiftMatch]:
    recalculated_at = now or datetime.now(UTC)
    requested_employees = set(employee_ids or [])
    schedules_by_key: dict[tuple[uuid.UUID, date], list[CourierScheduleEntry]] = defaultdict(list)
    shifts_by_key: dict[tuple[uuid.UUID, date], list[CourierIikoShift]] = defaultdict(list)

    for schedule in schedules:
        if requested_employees and schedule.courier_employee_id not in requested_employees:
            continue
        schedules_by_key[(schedule.courier_employee_id, schedule.work_date)].append(schedule)

    for shift in shifts:
        if shift.employee_id is None:
            continue
        if requested_employees and shift.employee_id not in requested_employees:
            continue
        work_date = local_work_date(shift.opened_at)
        if from_date <= work_date <= to_date:
            shifts_by_key[(shift.employee_id, work_date)].append(shift)

    keys = sorted(set(schedules_by_key) | set(shifts_by_key), key=lambda item: (item[1], item[0]))
    matches: list[CourierShiftMatch] = []
    for courier_id, work_date in keys:
        day_schedules = sorted(
            schedules_by_key.get((courier_id, work_date), []),
            key=lambda item: item.planned_start_at,
        )
        day_shifts = sorted(
            shifts_by_key.get((courier_id, work_date), []),
            key=lambda item: item.opened_at,
        )
        unused_shifts = list(day_shifts)

        for schedule in day_schedules:
            shift = pop_best_shift_for_schedule(unused_shifts, schedule)
            if shift is None:
                matches.append(
                    make_match(
                        courier_id=courier_id,
                        work_date=work_date,
                        schedule_entry_id=schedule.id,
                        iiko_shift_id=None,
                        status=no_show_status(schedule),
                        recalculated_at=recalculated_at,
                    )
                )
                continue

            worked_minutes = worked_minutes_for_shift(shift)
            status = planned_shift_status(schedule, worked_minutes)
            matches.append(
                make_match(
                    courier_id=courier_id,
                    work_date=work_date,
                    schedule_entry_id=schedule.id,
                    iiko_shift_id=shift.id,
                    status=status,
                    late_minutes=late_minutes(schedule, shift),
                    worked_minutes=worked_minutes,
                    deliveries_count=delivery_counts.get((courier_id, work_date)),
                    recalculated_at=recalculated_at,
                )
            )

        if day_schedules:
            continue

        for shift in unused_shifts:
            worked_minutes = worked_minutes_for_shift(shift)
            if worked_minutes is None or worked_minutes < MIN_WORKED_MINUTES:
                continue
            deliveries_count = delivery_counts.get((courier_id, work_date), 0)
            status = (
                CourierShiftMatchStatus.HELPING
                if deliveries_count >= 1
                else CourierShiftMatchStatus.NOT_COUNTED
            )
            matches.append(
                make_match(
                    courier_id=courier_id,
                    work_date=work_date,
                    schedule_entry_id=None,
                    iiko_shift_id=shift.id,
                    status=status,
                    worked_minutes=worked_minutes,
                    deliveries_count=deliveries_count,
                    recalculated_at=recalculated_at,
                )
            )

    return matches


async def recalculate_matches(
    session: AsyncSession,
    from_date: date,
    to_date: date,
    employee_ids: Iterable[uuid.UUID] | None = None,
) -> MatchRecalculationReport:
    if from_date > to_date:
        raise ValueError("from_date must be before or equal to to_date")

    requested_employees = set(employee_ids or [])
    schedules = await load_schedules(session, from_date, to_date, requested_employees)
    shifts = await load_iiko_shifts(session, from_date, to_date, requested_employees)
    employee_ids_for_counts = {schedule.courier_employee_id for schedule in schedules} | {
        shift.employee_id for shift in shifts if shift.employee_id is not None
    }
    employees = await load_employees(session, employee_ids_for_counts)
    delivery_counts = await load_delivery_counts(session, employees, from_date, to_date)

    matches = build_shift_matches(
        schedules=schedules,
        shifts=shifts,
        delivery_counts=delivery_counts,
        from_date=from_date,
        to_date=to_date,
        employee_ids=requested_employees or None,
    )
    report = report_for_matches(matches)
    existing = await load_existing_matches(session, from_date, to_date, requested_employees)
    existing_by_key = {match_key(row): row for row in existing}
    desired_keys = {match_key(row) for row in matches}

    for row in existing:
        if match_key(row) not in desired_keys:
            await session.delete(row)
            report.deleted += 1

    for desired in matches:
        existing_row = existing_by_key.get(match_key(desired))
        if existing_row is None:
            session.add(desired)
            report.inserted += 1
        else:
            apply_match(existing_row, desired)
            report.updated += 1

    await session.flush()
    return report


async def load_schedules(
    session: AsyncSession,
    from_date: date,
    to_date: date,
    employee_ids: set[uuid.UUID],
) -> list[CourierScheduleEntry]:
    stmt = select(CourierScheduleEntry).where(
        CourierScheduleEntry.work_date >= from_date,
        CourierScheduleEntry.work_date <= to_date,
    )
    if employee_ids:
        stmt = stmt.where(CourierScheduleEntry.courier_employee_id.in_(employee_ids))
    result = await session.scalars(stmt)
    return list(result.all())


async def load_iiko_shifts(
    session: AsyncSession,
    from_date: date,
    to_date: date,
    employee_ids: set[uuid.UUID],
) -> list[CourierIikoShift]:
    start_at = datetime.combine(from_date, datetime.min.time(), tzinfo=MOSCOW_TZ)
    end_at = datetime.combine(to_date + timedelta(days=1), datetime.min.time(), tzinfo=MOSCOW_TZ)
    stmt = select(CourierIikoShift).where(
        CourierIikoShift.employee_id.is_not(None),
        CourierIikoShift.opened_at >= start_at,
        CourierIikoShift.opened_at < end_at,
    )
    if employee_ids:
        stmt = stmt.where(CourierIikoShift.employee_id.in_(employee_ids))
    result = await session.scalars(stmt)
    return list(result.all())


async def load_employees(
    session: AsyncSession,
    employee_ids: set[uuid.UUID],
) -> list[Employee]:
    if not employee_ids:
        return []
    result = await session.scalars(select(Employee).where(Employee.id.in_(employee_ids)))
    return list(result.all())


async def load_delivery_counts(
    session: AsyncSession,
    employees: Sequence[Employee],
    from_date: date,
    to_date: date,
) -> dict[tuple[uuid.UUID, date], int]:
    employee_by_iiko_id = {employee.iiko_id: employee for employee in employees if employee.iiko_id}
    if not employee_by_iiko_id:
        return {}

    status_lower = func.lower(DeliveryOrder.status)
    result = await session.execute(
        select(
            DeliveryOrder.courier_iiko_id,
            DeliveryOrder.work_date,
            func.count(DeliveryOrder.id),
        )
        .where(
            DeliveryOrder.courier_iiko_id.in_(list(employee_by_iiko_id)),
            DeliveryOrder.work_date >= from_date,
            DeliveryOrder.work_date <= to_date,
            or_(
                DeliveryOrder.status.is_(None),
                status_lower.notin_(NON_DELIVERY_STATUSES),
            ),
        )
        .group_by(DeliveryOrder.courier_iiko_id, DeliveryOrder.work_date)
    )
    counts: dict[tuple[uuid.UUID, date], int] = {}
    for courier_iiko_id, work_date, count in result.all():
        employee = employee_by_iiko_id.get(courier_iiko_id)
        if employee is not None:
            counts[(employee.id, work_date)] = int(count)
    return counts


async def load_existing_matches(
    session: AsyncSession,
    from_date: date,
    to_date: date,
    employee_ids: set[uuid.UUID],
) -> list[CourierShiftMatch]:
    stmt = select(CourierShiftMatch).where(
        CourierShiftMatch.work_date >= from_date,
        CourierShiftMatch.work_date <= to_date,
    )
    if employee_ids:
        stmt = stmt.where(CourierShiftMatch.courier_employee_id.in_(employee_ids))
    result = await session.scalars(stmt)
    return list(result.all())


async def clear_matches_for_deleted_schedule_entry(
    session: AsyncSession,
    schedule_entry_id: int,
) -> None:
    await session.execute(
        delete(CourierShiftMatch).where(CourierShiftMatch.schedule_entry_id == schedule_entry_id)
    )


def report_for_matches(matches: Sequence[CourierShiftMatch]) -> MatchRecalculationReport:
    report = MatchRecalculationReport()
    for match in matches:
        if match.status == CourierShiftMatchStatus.MATCHED_PRIMARY:
            report.matched_primary += 1
        elif match.status == CourierShiftMatchStatus.SHORT_PRIMARY:
            report.short_primary += 1
        elif match.status == CourierShiftMatchStatus.NO_SHOW_PRIMARY:
            report.no_show_primary += 1
        elif match.status == CourierShiftMatchStatus.MATCHED_SECONDARY:
            report.matched_secondary += 1
        elif match.status == CourierShiftMatchStatus.SHORT_SECONDARY:
            report.short_secondary += 1
        elif match.status == CourierShiftMatchStatus.NO_SHOW_SECONDARY:
            report.no_show_secondary += 1
        elif match.status == CourierShiftMatchStatus.HELPING:
            report.helping += 1
        elif match.status == CourierShiftMatchStatus.NOT_COUNTED:
            report.not_counted += 1
    return report


def planned_shift_status(
    schedule: CourierScheduleEntry,
    worked_minutes: int | None,
) -> CourierShiftMatchStatus:
    if worked_minutes is not None and worked_minutes >= MIN_WORKED_MINUTES:
        return (
            CourierShiftMatchStatus.MATCHED_PRIMARY
            if schedule.category == "primary"
            else CourierShiftMatchStatus.MATCHED_SECONDARY
        )
    return (
        CourierShiftMatchStatus.SHORT_PRIMARY
        if schedule.category == "primary"
        else CourierShiftMatchStatus.SHORT_SECONDARY
    )


def no_show_status(schedule: CourierScheduleEntry) -> CourierShiftMatchStatus:
    return (
        CourierShiftMatchStatus.NO_SHOW_PRIMARY
        if schedule.category == "primary"
        else CourierShiftMatchStatus.NO_SHOW_SECONDARY
    )


def pop_best_shift_for_schedule(
    shifts: list[CourierIikoShift],
    schedule: CourierScheduleEntry,
) -> CourierIikoShift | None:
    if not shifts:
        return None
    best = min(
        shifts,
        key=lambda shift: abs((shift.opened_at - schedule.planned_start_at).total_seconds()),
    )
    shifts.remove(best)
    return best


def make_match(
    *,
    courier_id: uuid.UUID,
    work_date: date,
    schedule_entry_id: int | None,
    iiko_shift_id: int | None,
    status: CourierShiftMatchStatus,
    recalculated_at: datetime,
    late_minutes: int | None = None,
    worked_minutes: int | None = None,
    deliveries_count: int | None = None,
) -> CourierShiftMatch:
    return CourierShiftMatch(
        courier_employee_id=courier_id,
        work_date=work_date,
        schedule_entry_id=schedule_entry_id,
        iiko_shift_id=iiko_shift_id,
        status=status,
        late_minutes=late_minutes,
        worked_minutes=worked_minutes,
        deliveries_count=deliveries_count,
        recalculated_at=recalculated_at,
    )


def apply_match(target: CourierShiftMatch, source: CourierShiftMatch) -> None:
    target.status = source.status
    target.late_minutes = source.late_minutes
    target.worked_minutes = source.worked_minutes
    target.deliveries_count = source.deliveries_count
    target.recalculated_at = source.recalculated_at


def match_key(match: CourierShiftMatch) -> tuple[uuid.UUID, date, int | None, int | None]:
    return (
        match.courier_employee_id,
        match.work_date,
        match.schedule_entry_id,
        match.iiko_shift_id,
    )


def local_work_date(opened_at: datetime) -> date:
    return opened_at.astimezone(MOSCOW_TZ).date()


def worked_minutes_for_shift(shift: CourierIikoShift) -> int | None:
    if shift.closed_at is None:
        return None
    seconds = (shift.closed_at - shift.opened_at).total_seconds()
    if seconds < 0:
        return None
    return int(seconds // 60)


def late_minutes(schedule: CourierScheduleEntry, shift: CourierIikoShift) -> int:
    seconds = (shift.opened_at - schedule.planned_start_at).total_seconds()
    return max(0, int(seconds // 60))

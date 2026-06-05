from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CourierIikoShift,
    CourierScheduleEntry,
    CourierShiftMatch,
    DeliveryOrder,
    Employee,
)
from app.schemas.couriers import (
    CourierDisciplineBreakdown,
    CourierKPI,
    CourierShiftBrief,
    KpiValue,
)
from app.services.couriers.common import get_courier_or_404
from app.services.couriers.shift_matching import (
    MOSCOW_TZ,
    NON_DELIVERY_STATUSES,
)


async def get_courier_kpi(
    session: AsyncSession,
    courier_id: uuid.UUID,
    month: date,
) -> CourierKPI:
    """KPI одного курьера за месяц."""

    courier = await get_courier_or_404(session, courier_id)
    return await _kpi_for_employee(session, courier, month)


async def list_couriers_kpi(
    session: AsyncSession,
    month: date,
) -> list[CourierKPI]:
    """KPI всех активных курьеров за месяц."""

    employees = await _active_couriers(session)
    rows: list[CourierKPI] = []
    for employee in employees:
        row = await _kpi_for_employee(session, employee, month)
        rows.append(row)
    return rows


async def get_discipline_breakdown(
    session: AsyncSession,
    courier_id: uuid.UUID,
    month: date,
) -> CourierDisciplineBreakdown:
    month_start, month_end = month_bounds(month)
    matches = await _shift_matches(session, courier_id, month_start, month_end)
    return _discipline_breakdown(matches)


async def get_last_shift(
    session: AsyncSession,
    courier_id: uuid.UUID,
    month: date,
) -> CourierShiftBrief | None:
    month_start, month_end = month_bounds(month)
    result = await session.scalars(
        select(CourierIikoShift)
        .where(
            CourierIikoShift.employee_id == courier_id,
            CourierIikoShift.opened_at
            >= datetime.combine(month_start, datetime.min.time(), tzinfo=MOSCOW_TZ),
            CourierIikoShift.opened_at
            < datetime.combine(month_end, datetime.min.time(), tzinfo=MOSCOW_TZ),
        )
        .order_by(CourierIikoShift.opened_at.desc())
        .limit(1)
    )
    shift = result.first()
    if shift is None:
        return None
    return CourierShiftBrief(
        id=shift.id,
        opened_at=shift.opened_at,
        closed_at=shift.closed_at,
        attendance_type=shift.attendance_type,
        worked_minutes=_minutes_between(shift.opened_at, shift.closed_at)
        if shift.closed_at is not None
        else None,
    )


async def month_schedule_counts(
    session: AsyncSession,
    courier_id: uuid.UUID,
    month: date,
) -> tuple[int, int]:
    month_start, month_end = month_bounds(month)
    schedules = await _schedule_entries(session, courier_id, month_start, month_end)
    return (
        sum(1 for schedule in schedules if schedule.category == "primary"),
        sum(1 for schedule in schedules if schedule.category == "secondary"),
    )


async def has_open_shift_now(session: AsyncSession, courier_id: uuid.UUID) -> bool:
    shift_id = await session.scalar(
        select(CourierIikoShift.id)
        .where(
            CourierIikoShift.employee_id == courier_id,
            CourierIikoShift.closed_at.is_(None),
        )
        .limit(1)
    )
    return shift_id is not None


def month_bounds(month: date) -> tuple[date, date]:
    month_start = month.replace(day=1)
    if month_start.month == 12:
        return month_start, date(month_start.year + 1, 1, 1)
    return month_start, date(month_start.year, month_start.month + 1, 1)


async def _active_couriers(session: AsyncSession) -> list[Employee]:
    result = await session.scalars(
        select(Employee)
        .where(Employee.position == "Курьер", Employee.status == "active")
        .order_by(Employee.full_name)
    )
    return list(result.all())


async def _kpi_for_employee(
    session: AsyncSession,
    courier: Employee,
    month: date,
) -> CourierKPI:
    month_start, month_end = month_bounds(month)
    delivery_rows = await _delivery_rows(session, courier.iiko_id, month_start, month_end)
    matches = await _shift_matches(session, courier.id, month_start, month_end)
    schedules = await _schedule_entries(session, courier.id, month_start, month_end)
    breakdown = _discipline_breakdown(matches)

    deliveries_total = len(delivery_rows)
    primary_shifts_planned = sum(1 for schedule in schedules if schedule.category == "primary")
    primary_shifts_worked = sum(
        1 for match in matches if _status_value(match.status) == "matched_primary"
    )
    secondary_shifts_worked = sum(
        1 for match in matches if _status_value(match.status) == "matched_secondary"
    )
    primary_productivity_denominator = sum(
        1
        for match in matches
        if _status_value(match.status) in {"matched_primary", "short_primary"}
    )
    primary_deliveries = _deliveries_in_primary_shifts(delivery_rows, schedules)
    speed = _speed_value(delivery_rows)
    discipline = _discipline_value(primary_shifts_worked, primary_shifts_planned)
    productivity = _productivity_value(primary_deliveries, primary_productivity_denominator)

    return CourierKPI(
        courier_id=courier.id,
        courier_name=courier.full_name,
        speed_minutes=speed,
        discipline_percent=discipline,
        productivity=productivity,
        help_count=breakdown.help,
        deliveries_total=deliveries_total,
        primary_shifts_worked=primary_shifts_worked,
        secondary_shifts_worked=secondary_shifts_worked,
        primary_shifts_planned=primary_shifts_planned,
    )


async def _delivery_rows(
    session: AsyncSession,
    iiko_id: str | None,
    month_start: date,
    month_end: date,
) -> list[DeliveryOrder]:
    if not iiko_id:
        return []
    status_lower = func.lower(DeliveryOrder.status)
    result = await session.scalars(
        select(DeliveryOrder)
        .where(
            DeliveryOrder.courier_iiko_id == iiko_id,
            DeliveryOrder.work_date >= month_start,
            DeliveryOrder.work_date < month_end,
            or_(
                DeliveryOrder.status.is_(None),
                status_lower.notin_(NON_DELIVERY_STATUSES),
            ),
        )
        .order_by(DeliveryOrder.work_date)
    )
    return list(result.all())


async def _shift_matches(
    session: AsyncSession,
    courier_id: uuid.UUID,
    month_start: date,
    month_end: date,
) -> list[CourierShiftMatch]:
    result = await session.scalars(
        select(CourierShiftMatch)
        .where(
            CourierShiftMatch.courier_employee_id == courier_id,
            CourierShiftMatch.work_date >= month_start,
            CourierShiftMatch.work_date < month_end,
        )
        .order_by(CourierShiftMatch.work_date)
    )
    return list(result.all())


async def _schedule_entries(
    session: AsyncSession,
    courier_id: uuid.UUID,
    month_start: date,
    month_end: date,
) -> list[CourierScheduleEntry]:
    result = await session.scalars(
        select(CourierScheduleEntry)
        .where(
            CourierScheduleEntry.courier_employee_id == courier_id,
            CourierScheduleEntry.work_date >= month_start,
            CourierScheduleEntry.work_date < month_end,
        )
        .order_by(CourierScheduleEntry.work_date)
    )
    return list(result.all())


def _discipline_breakdown(matches: list[CourierShiftMatch]) -> CourierDisciplineBreakdown:
    return CourierDisciplineBreakdown(
        planned=sum(
            1
            for match in matches
            if _status_value(match.status)
            in {"matched_primary", "short_primary", "no_show_primary"}
        ),
        worked=sum(1 for match in matches if _status_value(match.status) == "matched_primary"),
        help=sum(1 for match in matches if _helping_shift(match)),
        no_show=sum(1 for match in matches if _status_value(match.status) == "no_show_primary"),
    )


def _speed_value(delivery_rows: list[DeliveryOrder]) -> KpiValue:
    values: list[float] = []
    for row in delivery_rows:
        minutes = _delivery_minutes(row)
        if minutes is not None:
            values.append(minutes)
    if not values:
        return KpiValue(value=None, threshold=None)
    average = round(sum(values) / len(values), 1)
    return KpiValue(value=average, threshold=_speed_threshold(average))


def _discipline_value(primary_worked: int, primary_planned: int) -> KpiValue:
    if primary_planned <= 0:
        return KpiValue(value=None, threshold=None)
    value = round((primary_worked / primary_planned) * 100, 1)
    return KpiValue(value=value, threshold=_discipline_threshold(value))


def _productivity_value(deliveries_total: int, primary_shift_count: int) -> KpiValue:
    if primary_shift_count <= 0:
        return KpiValue(value=None, threshold=None)
    value = round(deliveries_total / primary_shift_count, 1)
    return KpiValue(value=value, threshold=_productivity_threshold(value))


def _deliveries_in_primary_shifts(
    delivery_rows: list[DeliveryOrder],
    schedules: list[CourierScheduleEntry],
) -> int:
    primary_windows = [
        (schedule.planned_start_at, schedule.planned_end_at)
        for schedule in schedules
        if schedule.category == "primary"
    ]
    if not primary_windows:
        return 0
    count = 0
    for row in delivery_rows:
        taken_at = getattr(row, "taken_at", None) or row.on_way_at
        if taken_at is None:
            continue
        if any(
            _datetime_in_window(taken_at, start_at, end_at) for start_at, end_at in primary_windows
        ):
            count += 1
    return count


def _datetime_in_window(value: datetime, start_at: datetime, end_at: datetime) -> bool:
    try:
        return start_at <= value <= end_at
    except TypeError:
        if value.tzinfo is None and start_at.tzinfo is not None:
            value = value.replace(tzinfo=start_at.tzinfo)
        elif value.tzinfo is not None and start_at.tzinfo is None:
            start_at = start_at.replace(tzinfo=value.tzinfo)
            end_at = end_at.replace(tzinfo=value.tzinfo)
        return start_at <= value <= end_at


def _delivery_minutes(row: DeliveryOrder) -> float | None:
    taken_at = getattr(row, "taken_at", None) or row.on_way_at
    delivered_at = getattr(row, "delivered_at", None) or row.closed_at
    if taken_at is not None and delivered_at is not None:
        minutes = _minutes_between(taken_at, delivered_at)
        if minutes is not None:
            return float(minutes)
    if row.way_duration_minutes is not None:
        return _decimal_to_float(row.way_duration_minutes)
    return None


def _helping_shift(match: CourierShiftMatch) -> bool:
    return _status_value(match.status) == "helping" and (match.deliveries_count or 0) >= 1


def _speed_threshold(value: float) -> str:
    if value <= 22:
        return "green"
    if value <= 35:
        return "yellow"
    return "red"


def _discipline_threshold(value: float) -> str:
    if value >= 80:
        return "green"
    if value >= 65:
        return "yellow"
    return "red"


def _productivity_threshold(value: float) -> str:
    if value >= 16:
        return "green"
    if value >= 10:
        return "yellow"
    return "red"


def _minutes_between(start: datetime, end: datetime | None) -> int | None:
    if end is None:
        return None
    try:
        return max(int((end - start).total_seconds() // 60), 0)
    except TypeError:
        if start.tzinfo is not None and end.tzinfo is None:
            end = end.replace(tzinfo=start.tzinfo)
        elif start.tzinfo is None and end.tzinfo is not None:
            start = start.replace(tzinfo=end.tzinfo)
        return max(int((end - start).total_seconds() // 60), 0)


def _decimal_to_float(value: Decimal | float | int) -> float:
    return float(value)


def _status_value(value: Any) -> str:
    return getattr(value, "value", value)

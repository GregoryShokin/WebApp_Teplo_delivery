from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor
from app.models import Employee, ScheduledShift, ShiftSchedule
from app.services import employee_assignments, vacation_service
from app.services.staff_taxonomy import (
    PAYROLL_ROLE_LABELS,
    default_station_for_payroll_role,
)

SCHEDULE_STATUSES = frozenset({"draft", "published", "superseded"})
SCHEDULE_EMPLOYEE_POSITIONS = frozenset({"Повар", "Кассир"})
MAX_SHIFT_HOURS = Decimal("16")
LOCATION_TZ = ZoneInfo("Europe/Moscow")
DEFAULT_SHIFT_START = time(10, 0)
DEFAULT_SHIFT_END = time(22, 0)


async def create_schedule(
    session: AsyncSession,
    *,
    date_start: date,
    date_end: date,
    notes: str | None,
    actor: CurrentActor,
) -> ShiftSchedule:
    if date_end < date_start:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Дата окончания не может быть раньше даты начала",
        )

    schedule = ShiftSchedule(
        id=uuid.uuid4(),
        date_start=date_start,
        date_end=date_end,
        status="draft",
        notes=notes,
        created_by_user_id=_actor_user_id(actor),
    )
    session.add(schedule)
    await _commit_refresh(session, schedule)
    return schedule


async def get_schedule_with_shifts(
    session: AsyncSession,
    schedule_id: uuid.UUID,
) -> tuple[ShiftSchedule, list[tuple[ScheduledShift, Employee]]]:
    schedule = await session.get(ShiftSchedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="График не найден")

    result = await session.execute(
        select(ScheduledShift, Employee)
        .join(Employee, Employee.id == ScheduledShift.employee_id)
        .where(ScheduledShift.shift_schedule_id == schedule_id)
        .order_by(ScheduledShift.business_date, Employee.full_name)
    )
    rows = [
        (shift, employee)
        for shift, employee in result.all()
        if shift.shift_schedule_id == schedule_id
    ]
    return schedule, rows


async def list_schedules(
    session: AsyncSession,
    *,
    status: str | None,
    date_from: date | None,
    date_to: date | None,
) -> list[ShiftSchedule]:
    if status is not None and status not in SCHEDULE_STATUSES:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Invalid schedule status",
        )
    statement = select(ShiftSchedule)
    if status is not None:
        statement = statement.where(ShiftSchedule.status == status)
    if date_from is not None:
        statement = statement.where(ShiftSchedule.date_end >= date_from)
    if date_to is not None:
        statement = statement.where(ShiftSchedule.date_start <= date_to)
    statement = statement.order_by(ShiftSchedule.status, ShiftSchedule.date_start.desc())
    schedules = list((await session.scalars(statement)).all())
    return [
        schedule
        for schedule in schedules
        if _schedule_matches_filters(schedule, status=status, date_from=date_from, date_to=date_to)
    ]


async def update_schedule(
    session: AsyncSession,
    schedule_id: uuid.UUID,
    *,
    notes: str | None,
    actor: CurrentActor,
) -> ShiftSchedule:
    del actor
    schedule = await session.get(ShiftSchedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="График не найден")
    schedule.notes = notes
    schedule.updated_at = datetime.now(UTC)
    await _commit_refresh(session, schedule)
    return schedule


async def publish_schedule(
    session: AsyncSession,
    schedule_id: uuid.UUID,
    *,
    actor: CurrentActor,
) -> ShiftSchedule:
    schedule = await session.get(ShiftSchedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="График не найден")
    if schedule.status == "published":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="График уже опубликован",
        )
    if schedule.status != "draft":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Опубликовать можно только черновик",
        )

    result = await session.scalars(
        select(ShiftSchedule).where(
            ShiftSchedule.id != schedule.id,
            ShiftSchedule.status == "published",
            ShiftSchedule.date_start <= schedule.date_end,
            ShiftSchedule.date_end >= schedule.date_start,
        )
    )
    overlapping = [
        existing
        for existing in result.all()
        if existing.id != schedule.id
        and existing.status == "published"
        and _ranges_overlap(
            existing.date_start,
            existing.date_end,
            schedule.date_start,
            schedule.date_end,
        )
    ]

    now = datetime.now(UTC)
    for existing in overlapping:
        existing.status = "superseded"
        existing.superseded_by_id = schedule.id
        existing.updated_at = now

    schedule.status = "published"
    schedule.published_at = now
    schedule.published_by_user_id = _actor_user_id(actor)
    schedule.updated_at = now
    await _commit_refresh(session, schedule)
    return schedule


async def create_new_version(
    session: AsyncSession,
    source_id: uuid.UUID,
    *,
    actor: CurrentActor,
) -> ShiftSchedule:
    source = await session.get(ShiftSchedule, source_id)
    if source is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="График не найден")
    if source.status != "published":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Новую версию можно создать только из опубликованного графика",
        )

    clone = ShiftSchedule(
        id=uuid.uuid4(),
        date_start=source.date_start,
        date_end=source.date_end,
        status="draft",
        notes=source.notes,
        created_by_user_id=_actor_user_id(actor),
    )
    session.add(clone)
    await session.flush()

    shifts = await _list_shifts_for_schedule(session, source.id)
    for shift in shifts:
        session.add(
            ScheduledShift(
                id=uuid.uuid4(),
                shift_schedule_id=clone.id,
                business_date=shift.business_date,
                employee_id=shift.employee_id,
                payroll_role=shift.payroll_role,
                station_code=shift.station_code,
                planned_start_at=shift.planned_start_at,
                planned_end_at=shift.planned_end_at,
                comment_private=shift.comment_private,
                created_by_user_id=_actor_user_id(actor),
            )
        )

    await _commit_refresh(session, clone)
    return clone


async def upsert_shift(
    session: AsyncSession,
    schedule_id: uuid.UUID,
    *,
    business_date: date,
    employee_id: uuid.UUID,
    payroll_role: str | None = None,
    station_code: str | None = None,
    planned_start_at: datetime | None = None,
    planned_end_at: datetime | None = None,
    comment_private: str | None,
    actor: CurrentActor,
) -> ScheduledShift:
    schedule = await _get_draft_schedule(session, schedule_id)
    employee = await _get_schedulable_employee(session, employee_id)
    await _ensure_employee_not_on_vacation(session, employee_id, business_date)
    resolved_role, resolved_station, resolved_start, resolved_end = await _resolve_shift_fields(
        session,
        employee=employee,
        business_date=business_date,
        payroll_role=payroll_role,
        station_code=station_code,
        planned_start_at=planned_start_at,
        planned_end_at=planned_end_at,
    )
    _validate_shift_payload(
        schedule,
        business_date=business_date,
        planned_start_at=resolved_start,
        planned_end_at=resolved_end,
    )

    existing = await _find_shift_for_employee_day(
        session,
        schedule_id=schedule_id,
        business_date=business_date,
        employee_id=employee_id,
    )
    if existing is None:
        existing = ScheduledShift(
            id=uuid.uuid4(),
            shift_schedule_id=schedule_id,
            business_date=business_date,
            employee_id=employee_id,
            created_by_user_id=_actor_user_id(actor),
        )
        session.add(existing)

    _assign_shift_fields(
        existing,
        payroll_role=resolved_role,
        station_code=resolved_station,
        planned_start_at=resolved_start,
        planned_end_at=resolved_end,
        comment_private=comment_private,
    )
    await _commit_refresh(session, existing)
    return existing


async def update_shift(
    session: AsyncSession,
    schedule_id: uuid.UUID,
    shift_id: uuid.UUID,
    *,
    business_date: date,
    employee_id: uuid.UUID,
    payroll_role: str | None = None,
    station_code: str | None = None,
    planned_start_at: datetime | None = None,
    planned_end_at: datetime | None = None,
    comment_private: str | None,
    actor: CurrentActor,
) -> ScheduledShift:
    schedule = await _get_draft_schedule(session, schedule_id)
    shift = await session.get(ScheduledShift, shift_id)
    if shift is None or shift.shift_schedule_id != schedule_id:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Смена не найдена")
    employee = await _get_schedulable_employee(session, employee_id)
    await _ensure_employee_not_on_vacation(session, employee_id, business_date)
    resolved_role, resolved_station, resolved_start, resolved_end = await _resolve_shift_fields(
        session,
        employee=employee,
        business_date=business_date,
        payroll_role=payroll_role,
        station_code=station_code,
        planned_start_at=planned_start_at,
        planned_end_at=planned_end_at,
    )
    _validate_shift_payload(
        schedule,
        business_date=business_date,
        planned_start_at=resolved_start,
        planned_end_at=resolved_end,
    )
    shift.business_date = business_date
    shift.employee_id = employee_id
    shift.created_by_user_id = shift.created_by_user_id or _actor_user_id(actor)
    _assign_shift_fields(
        shift,
        payroll_role=resolved_role,
        station_code=resolved_station,
        planned_start_at=resolved_start,
        planned_end_at=resolved_end,
        comment_private=comment_private,
    )
    await _commit_refresh(session, shift)
    return shift


async def delete_shift(
    session: AsyncSession,
    shift_id: uuid.UUID,
    *,
    schedule_id: uuid.UUID | None = None,
    actor: CurrentActor,
) -> None:
    del actor
    shift = await session.get(ScheduledShift, shift_id)
    if shift is None or (schedule_id is not None and shift.shift_schedule_id != schedule_id):
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Смена не найдена")
    await _get_draft_schedule(session, shift.shift_schedule_id)
    await session.delete(shift)
    await session.commit()


async def delete_schedule(
    session: AsyncSession,
    schedule_id: uuid.UUID,
    *,
    actor: CurrentActor,
) -> None:
    del actor
    schedule = await session.get(ShiftSchedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="График не найден")
    if schedule.status != "draft":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Удалить можно только черновик",
        )
    await session.delete(schedule)
    await session.commit()


async def bulk_copy_week(
    session: AsyncSession,
    schedule_id: uuid.UUID,
    *,
    from_date: date,
    to_date: date,
    actor: CurrentActor,
) -> int:
    schedule = await _get_draft_schedule(session, schedule_id)
    source_dates = {from_date + timedelta(days=offset) for offset in range(7)}
    target_dates = {to_date + timedelta(days=offset) for offset in range(7)}
    if min(target_dates) < schedule.date_start or max(target_dates) > schedule.date_end:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Целевая неделя вне периода графика",
        )
    shifts = [
        shift
        for shift in await _list_shifts_for_schedule(session, schedule_id)
        if shift.business_date in source_dates
    ]
    delta = to_date - from_date
    copied = 0

    for source in shifts:
        target_date = source.business_date + delta
        await _ensure_employee_not_on_vacation(session, source.employee_id, target_date)
        target = await _find_shift_for_employee_day(
            session,
            schedule_id=schedule_id,
            business_date=target_date,
            employee_id=source.employee_id,
        )
        if target is None:
            target = ScheduledShift(
                id=uuid.uuid4(),
                shift_schedule_id=schedule_id,
                business_date=target_date,
                employee_id=source.employee_id,
                created_by_user_id=_actor_user_id(actor),
            )
            session.add(target)
        target.payroll_role = source.payroll_role
        target.station_code = source.station_code
        target.planned_start_at = source.planned_start_at + delta
        target.planned_end_at = source.planned_end_at + delta
        target.comment_private = source.comment_private
        target.updated_at = datetime.now(UTC)
        copied += 1

    await _commit(session)
    return copied


async def list_employees_roster(session: AsyncSession) -> list[dict[str, Any]]:
    today = date.today()
    result = await session.scalars(
        select(Employee)
        .where(
            Employee.position.in_(SCHEDULE_EMPLOYEE_POSITIONS),
            Employee.status == "active",
        )
        .order_by(Employee.full_name)
    )
    roster: list[dict[str, Any]] = []
    for employee in result.all():
        if employee.position not in SCHEDULE_EMPLOYEE_POSITIONS or employee.status != "active":
            continue
        assignments = await employee_assignments.get_assignments(session, employee.id, today)
        primary = next((assignment for assignment in assignments if assignment.is_primary), None)
        roster.append(
            {
                "id": employee.id,
                "full_name": employee.full_name,
                "position": employee.position,
                "primary_payroll_role": primary.payroll_role if primary else None,
                "default_cooking_station": employee.default_cooking_station,
                "available_roles": [
                    {
                        "payroll_role": assignment.payroll_role,
                        "category": assignment.category,
                        "is_primary": bool(assignment.is_primary),
                        "default_station_code": default_station_for_payroll_role(
                            assignment.payroll_role
                        ),
                    }
                    for assignment in assignments
                ],
                "allowances": {
                    "senior": bool(employee.is_senior),
                    "deputy": bool(employee.is_deputy_senior),
                },
            },
        )
    return sorted(roster, key=lambda row: row["full_name"])


def planned_hours(start: datetime, end: datetime) -> Decimal:
    hours = Decimal(str((end - start).total_seconds() / 3600)).quantize(Decimal("0.01"))
    return hours.normalize()


async def _get_draft_schedule(session: AsyncSession, schedule_id: uuid.UUID) -> ShiftSchedule:
    schedule = await session.get(ShiftSchedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="График не найден")
    if schedule.status != "draft":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="График опубликован, создайте новую версию",
        )
    return schedule


async def _get_schedulable_employee(session: AsyncSession, employee_id: uuid.UUID) -> Employee:
    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Сотрудник не найден",
        )
    if employee.status != "active":
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="В график можно ставить только активных сотрудников",
        )
    if employee.position not in SCHEDULE_EMPLOYEE_POSITIONS:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="В график можно ставить только Поваров и Кассиров",
        )
    return employee


async def _ensure_employee_not_on_vacation(
    session: AsyncSession,
    employee_id: uuid.UUID,
    business_date: date,
) -> None:
    if await vacation_service.employee_has_active_vacation(
        session,
        employee_id=employee_id,
        business_date=business_date,
    ):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Сотрудник в отпуске в этот день. Смену поставить нельзя.",
        )


async def _resolve_shift_fields(
    session: AsyncSession,
    *,
    employee: Employee,
    business_date: date,
    payroll_role: str | None,
    station_code: str | None,
    planned_start_at: datetime | None,
    planned_end_at: datetime | None,
) -> tuple[str, str | None, datetime, datetime]:
    assignments = await employee_assignments.get_assignments(session, employee.id, business_date)
    if not assignments:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="У сотрудника не назначена ни одна роль на эту дату. Назначьте роль в Штате.",
        )

    assignment_roles = {assignment.payroll_role for assignment in assignments}
    if payroll_role is not None:
        if payroll_role not in assignment_roles:
            available = ", ".join(_role_display_name(role) for role in sorted(assignment_roles))
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Роль «{_role_display_name(payroll_role)}» не назначена этому сотруднику."
                    f" Доступны: {available}"
                ),
            )
        resolved_role = payroll_role
    else:
        primary = next((assignment for assignment in assignments if assignment.is_primary), None)
        if primary is None:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "У сотрудника нет основной роли на эту дату. "
                    "Назначьте основную роль в Штате."
                ),
            )
        resolved_role = primary.payroll_role

    resolved_station = (
        station_code
        if station_code is not None
        else default_station_for_payroll_role(resolved_role)
    )
    resolved_start = planned_start_at or datetime.combine(
        business_date,
        DEFAULT_SHIFT_START,
        tzinfo=LOCATION_TZ,
    )

    return (
        resolved_role,
        resolved_station,
        resolved_start,
        planned_end_at or datetime.combine(business_date, DEFAULT_SHIFT_END, tzinfo=LOCATION_TZ),
    )


def _validate_shift_payload(
    schedule: ShiftSchedule,
    *,
    business_date: date,
    planned_start_at: datetime,
    planned_end_at: datetime,
) -> None:
    if business_date < schedule.date_start or business_date > schedule.date_end:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Дата смены вне периода графика",
        )
    if not _is_timezone_aware(planned_start_at) or not _is_timezone_aware(planned_end_at):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Время смены должно быть с часовым поясом",
        )
    if planned_end_at <= planned_start_at:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Конец смены должен быть позже начала",
        )
    if planned_hours(planned_start_at, planned_end_at) > MAX_SHIFT_HOURS:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Смена длиннее 16 часов — проверьте время",
        )
    allowed_start_dates = {
        business_date - timedelta(days=1),
        business_date,
        business_date + timedelta(days=1),
    }
    if planned_start_at.date() not in allowed_start_dates:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Начало смены должно попадать в дату смены или соседний день",
        )


async def _find_shift_for_employee_day(
    session: AsyncSession,
    *,
    schedule_id: uuid.UUID,
    business_date: date,
    employee_id: uuid.UUID,
) -> ScheduledShift | None:
    result = await session.scalars(
        select(ScheduledShift).where(
            ScheduledShift.shift_schedule_id == schedule_id,
            ScheduledShift.business_date == business_date,
            ScheduledShift.employee_id == employee_id,
        )
    )
    return next(
        (
            shift
            for shift in result.all()
            if shift.shift_schedule_id == schedule_id
            and shift.business_date == business_date
            and shift.employee_id == employee_id
        ),
        None,
    )


async def _list_shifts_for_schedule(
    session: AsyncSession,
    schedule_id: uuid.UUID,
) -> list[ScheduledShift]:
    result = await session.scalars(
        select(ScheduledShift)
        .where(ScheduledShift.shift_schedule_id == schedule_id)
        .order_by(ScheduledShift.business_date, ScheduledShift.employee_id)
    )
    return [shift for shift in result.all() if shift.shift_schedule_id == schedule_id]


def _assign_shift_fields(
    shift: ScheduledShift,
    *,
    payroll_role: str,
    station_code: str | None,
    planned_start_at: datetime,
    planned_end_at: datetime,
    comment_private: str | None,
) -> None:
    shift.payroll_role = payroll_role
    shift.station_code = station_code
    shift.planned_start_at = planned_start_at
    shift.planned_end_at = planned_end_at
    shift.comment_private = comment_private
    shift.updated_at = datetime.now(UTC)


async def _commit_refresh(session: AsyncSession, item: Any) -> None:
    await _commit(session)
    await session.refresh(item)


async def _commit(session: AsyncSession) -> None:
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="У этого сотрудника на эту дату уже есть смена; обновите существующую",
        ) from exc


def _schedule_matches_filters(
    schedule: ShiftSchedule,
    *,
    status: str | None,
    date_from: date | None,
    date_to: date | None,
) -> bool:
    if status is not None and schedule.status != status:
        return False
    if date_from is not None and schedule.date_end < date_from:
        return False
    return not (date_to is not None and schedule.date_start > date_to)


def _ranges_overlap(
    left_start: date,
    left_end: date,
    right_start: date,
    right_end: date,
) -> bool:
    return left_start <= right_end and left_end >= right_start


def _is_timezone_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _role_display_name(role: str) -> str:
    return PAYROLL_ROLE_LABELS.get(role, role)


def _actor_user_id(actor: CurrentActor) -> uuid.UUID | None:
    user_id = getattr(actor, "user_id", None)
    return user_id if isinstance(user_id, uuid.UUID) else None

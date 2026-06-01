from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_finance_manager_plus
from app.db.session import get_session
from app.models import Employee, ScheduledShift, ShiftSchedule
from app.schemas.shift_schedule import (
    CopyWeekRequest,
    CopyWeekResponse,
    EmployeeRosterRow,
    ScheduleCreateRequest,
    ScheduledShiftRead,
    ScheduledShiftUpsertRequest,
    SchedulePatchRequest,
    ScheduleRead,
)
from app.services import shift_schedule_service

router = APIRouter()


@router.get("/employees-roster", response_model=list[EmployeeRosterRow])
async def get_employees_roster(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[dict]:
    del actor
    return await shift_schedule_service.list_employees_roster(session)


@router.get("", response_model=list[ScheduleRead])
@router.get("/", response_model=list[ScheduleRead], include_in_schema=False)
async def get_schedules(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[ScheduleRead]:
    del actor
    schedules = await shift_schedule_service.list_schedules(
        session,
        status=status_filter,
        date_from=date_from,
        date_to=date_to,
    )
    return [_schedule_to_read(schedule) for schedule in schedules]


@router.post("", response_model=ScheduleRead)
@router.post("/", response_model=ScheduleRead, include_in_schema=False)
async def post_schedule(
    payload: ScheduleCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> ScheduleRead:
    require_finance_manager_plus(actor)
    schedule = await shift_schedule_service.create_schedule(
        session,
        date_start=payload.date_start,
        date_end=payload.date_end,
        notes=payload.notes,
        actor=actor,
    )
    return _schedule_to_read(schedule)


@router.get("/{schedule_id}", response_model=ScheduleRead)
async def get_schedule(
    schedule_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> ScheduleRead:
    del actor
    schedule, shift_rows = await shift_schedule_service.get_schedule_with_shifts(
        session,
        schedule_id,
    )
    return _schedule_to_read(
        schedule,
        shifts=[_shift_to_read(shift, employee) for shift, employee in shift_rows],
    )


@router.patch("/{schedule_id}", response_model=ScheduleRead)
async def patch_schedule(
    schedule_id: uuid.UUID,
    payload: SchedulePatchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> ScheduleRead:
    require_finance_manager_plus(actor)
    schedule = await shift_schedule_service.update_schedule(
        session,
        schedule_id,
        notes=payload.notes,
        actor=actor,
    )
    return _schedule_to_read(schedule)


@router.post("/{schedule_id}/publish", response_model=ScheduleRead)
async def post_publish_schedule(
    schedule_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> ScheduleRead:
    require_finance_manager_plus(actor)
    schedule = await shift_schedule_service.publish_schedule(session, schedule_id, actor=actor)
    return _schedule_to_read(schedule)


@router.post("/{schedule_id}/new-version", response_model=ScheduleRead)
async def post_new_version(
    schedule_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> ScheduleRead:
    require_finance_manager_plus(actor)
    schedule = await shift_schedule_service.create_new_version(session, schedule_id, actor=actor)
    return _schedule_to_read(schedule)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    schedule_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> Response:
    require_finance_manager_plus(actor)
    await shift_schedule_service.delete_schedule(session, schedule_id, actor=actor)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{schedule_id}/shifts", response_model=ScheduledShiftRead)
async def post_shift(
    schedule_id: uuid.UUID,
    payload: ScheduledShiftUpsertRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> ScheduledShiftRead:
    require_finance_manager_plus(actor)
    shift = await shift_schedule_service.upsert_shift(
        session,
        schedule_id,
        business_date=payload.business_date,
        employee_id=payload.employee_id,
        station_code=payload.station_code,
        planned_start_at=payload.planned_start_at,
        planned_end_at=payload.planned_end_at,
        comment_private=payload.comment_private,
        actor=actor,
    )
    employee = await session.get(Employee, shift.employee_id)
    return _shift_to_read(shift, employee)


@router.patch("/{schedule_id}/shifts/{shift_id}", response_model=ScheduledShiftRead)
async def patch_shift(
    schedule_id: uuid.UUID,
    shift_id: uuid.UUID,
    payload: ScheduledShiftUpsertRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> ScheduledShiftRead:
    require_finance_manager_plus(actor)
    shift = await shift_schedule_service.update_shift(
        session,
        schedule_id,
        shift_id,
        business_date=payload.business_date,
        employee_id=payload.employee_id,
        station_code=payload.station_code,
        planned_start_at=payload.planned_start_at,
        planned_end_at=payload.planned_end_at,
        comment_private=payload.comment_private,
        actor=actor,
    )
    employee = await session.get(Employee, shift.employee_id)
    return _shift_to_read(shift, employee)


@router.delete(
    "/{schedule_id}/shifts/{shift_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_shift(
    schedule_id: uuid.UUID,
    shift_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> Response:
    require_finance_manager_plus(actor)
    await shift_schedule_service.delete_shift(
        session,
        shift_id,
        schedule_id=schedule_id,
        actor=actor,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{schedule_id}/copy-week", response_model=CopyWeekResponse)
async def post_copy_week(
    schedule_id: uuid.UUID,
    payload: CopyWeekRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> CopyWeekResponse:
    require_finance_manager_plus(actor)
    copied = await shift_schedule_service.bulk_copy_week(
        session,
        schedule_id,
        from_date=payload.from_date,
        to_date=payload.to_date,
        actor=actor,
    )
    return CopyWeekResponse(copied=copied)


def _schedule_to_read(
    schedule: ShiftSchedule,
    *,
    shifts: list[ScheduledShiftRead] | None = None,
) -> ScheduleRead:
    return ScheduleRead(
        id=schedule.id,
        date_start=schedule.date_start,
        date_end=schedule.date_end,
        status=schedule.status,
        notes=schedule.notes,
        published_at=schedule.published_at,
        superseded_by_id=schedule.superseded_by_id,
        created_by_label=None,
        shifts=shifts or [],
    )


def _shift_to_read(
    shift: ScheduledShift,
    employee: Employee | None,
) -> ScheduledShiftRead:
    return ScheduledShiftRead(
        id=shift.id,
        business_date=shift.business_date,
        employee_id=shift.employee_id,
        employee_full_name=employee.full_name if employee is not None else "",
        payroll_role=shift.payroll_role,
        station_code=shift.station_code,
        planned_start_at=shift.planned_start_at,
        planned_end_at=shift.planned_end_at,
        planned_hours=shift_schedule_service.planned_hours(
            shift.planned_start_at,
            shift.planned_end_at,
        ),
        comment_private=shift.comment_private,
    )

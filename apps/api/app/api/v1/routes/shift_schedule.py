from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_finance_manager_plus
from app.db.session import get_session
from app.models import Employee, RevenueForecast, ScheduledShift, ShiftSchedule, User
from app.schemas.shift_schedule import (
    CopyWeekRequest,
    CopyWeekResponse,
    EmployeeRosterRow,
    RevenueForecastOverrideRequest,
    RevenueForecastRead,
    RevenueForecastRecomputeRequest,
    RevenueForecastRecomputeResponse,
    ScheduleCreateRequest,
    ScheduledShiftRead,
    ScheduledShiftUpsertRequest,
    SchedulePatchRequest,
    ScheduleRead,
)
from app.services import revenue_forecast_service, shift_schedule_service

router = APIRouter()
MAX_FORECAST_RANGE_DAYS = 62


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


@router.get("/forecast", response_model=list[RevenueForecastRead])
async def get_forecast_range(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    date_from: date,
    date_to: date,
) -> list[RevenueForecastRead]:
    del actor
    _validate_forecast_range(date_from, date_to)
    forecasts = await revenue_forecast_service.get_forecasts_in_range(
        session,
        date_from,
        date_to,
    )
    labels = await _manual_override_labels(session, forecasts)
    return [_forecast_to_read(forecast, labels) for forecast in forecasts]


@router.post("/forecast/recompute", response_model=RevenueForecastRecomputeResponse)
async def post_recompute_forecast(
    payload: RevenueForecastRecomputeRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> RevenueForecastRecomputeResponse:
    require_finance_manager_plus(actor)
    recomputed = await revenue_forecast_service.compute_forecast_for_range(
        session,
        payload.date_from,
        payload.date_to,
        force_refresh_iiko=payload.force_refresh_iiko,
    )
    return RevenueForecastRecomputeResponse(recomputed=len(recomputed))


@router.post("/forecast/{business_date}/override", response_model=RevenueForecastRead)
async def post_forecast_override(
    business_date: date,
    payload: RevenueForecastOverrideRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> RevenueForecastRead:
    require_finance_manager_plus(actor)
    forecast = await revenue_forecast_service.apply_manual_override(
        session,
        business_date,
        amount=payload.amount,
        reason=payload.reason,
        actor=actor,
    )
    labels = await _manual_override_labels(session, [forecast])
    return _forecast_to_read(forecast, labels)


@router.delete("/forecast/{business_date}/override", response_model=RevenueForecastRead)
async def delete_forecast_override(
    business_date: date,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> RevenueForecastRead:
    require_finance_manager_plus(actor)
    forecast = await revenue_forecast_service.remove_manual_override(
        session,
        business_date,
        actor=actor,
    )
    labels = await _manual_override_labels(session, [forecast])
    return _forecast_to_read(forecast, labels)


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


def _forecast_to_read(
    forecast: RevenueForecast,
    manual_override_labels: dict[uuid.UUID, str],
) -> RevenueForecastRead:
    label = None
    if forecast.manual_override_set_by_user_id is not None:
        label = manual_override_labels.get(forecast.manual_override_set_by_user_id)
    return RevenueForecastRead(
        business_date=forecast.business_date,
        weekday=forecast.weekday,
        method_code=forecast.method_code,
        history_window_weeks=forecast.history_window_weeks,
        history_points=forecast.history_points,
        base_average_amount=forecast.base_average_amount,
        season_coeff=forecast.season_coeff,
        event_coeff=forecast.event_coeff,
        manual_override_amount=forecast.manual_override_amount,
        manual_override_reason=forecast.manual_override_reason,
        manual_override_set_by_label=label,
        manual_override_set_at=forecast.manual_override_set_at,
        forecast_amount=forecast.forecast_amount,
        quality_status=forecast.quality_status,
        event_review_recommended=forecast.event_review_recommended,
        computed_at=forecast.computed_at,
    )


async def _manual_override_labels(
    session: AsyncSession,
    forecasts: list[RevenueForecast],
) -> dict[uuid.UUID, str]:
    user_ids = {
        forecast.manual_override_set_by_user_id
        for forecast in forecasts
        if forecast.manual_override_set_by_user_id is not None
    }
    if not user_ids:
        return {}
    result = await session.scalars(select(User).where(User.id.in_(user_ids)))
    return {user.id: user.full_name for user in result.all()}


def _validate_forecast_range(date_from: date, date_to: date) -> None:
    if date_to < date_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Дата окончания не может быть раньше даты начала",
        )
    if (date_to - date_from).days > MAX_FORECAST_RANGE_DAYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Период прогноза не может быть длиннее 62 дней",
        )

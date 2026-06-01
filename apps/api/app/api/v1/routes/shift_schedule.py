from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_finance_manager_plus
from app.db.session import get_session
from app.models import (
    Employee,
    PayrollForecastRun,
    RevenueForecast,
    ScheduledShift,
    ShiftAllowanceOverride,
    ShiftCostEstimate,
    ShiftSchedule,
    User,
)
from app.schemas.shift_schedule import (
    AllowanceAssignmentRead,
    AllowanceCandidateRead,
    CashierAllowanceOverrideRequest,
    CopyWeekRequest,
    CopyWeekResponse,
    EmployeeRosterRow,
    PayrollForecastRunRead,
    PlanFactDayRowRead,
    PlanFactEmployeeRowRead,
    PlanFactSummaryRead,
    RevenueForecastOverrideRequest,
    RevenueForecastRead,
    RevenueForecastRecomputeRequest,
    RevenueForecastRecomputeResponse,
    ScheduleCreateRequest,
    ScheduledShiftRead,
    ScheduledShiftUpsertRequest,
    SchedulePatchRequest,
    ScheduleRead,
    ShiftAllowanceOverrideRead,
    ShiftCostEstimateRead,
)
from app.services import (
    payroll_forecast_run_service,
    plan_fact_service,
    revenue_forecast_service,
    shift_schedule_service,
)
from app.services.seniority_allowance_resolver import (
    CASHIER_POSITION,
    AllowanceAssignment,
    delete_cashier_override,
    get_cashier_override,
    list_cashier_overrides,
    load_cashier_candidates,
    patch_cashier_override,
    recipient_full_name,
    resolve_cashier_allowance_recipient,
    upsert_cashier_override,
)

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


@router.get("/{schedule_id}/plan-fact", response_model=PlanFactSummaryRead)
async def get_plan_fact(
    schedule_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> PlanFactSummaryRead:
    del actor
    summary = await plan_fact_service.compute_plan_fact(session, schedule_id)
    await _attach_plan_fact_source_labels(session, summary)
    return _plan_fact_to_read(summary)


@router.get(
    "/{schedule_id}/cashier-allowance-overrides",
    response_model=list[ShiftAllowanceOverrideRead],
)
async def get_cashier_allowance_overrides(
    schedule_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    business_date: date | None = None,
) -> list[ShiftAllowanceOverrideRead]:
    del actor
    schedule = await _get_schedule(session, schedule_id)
    if business_date is not None:
        _validate_schedule_business_date(schedule, business_date)
    overrides = await list_cashier_overrides(
        session,
        shift_schedule_id=schedule_id,
        business_date=business_date,
    )
    return [_cashier_override_to_read(item) for item in overrides]


@router.post(
    "/{schedule_id}/cashier-allowance-overrides",
    response_model=ShiftAllowanceOverrideRead,
)
async def post_cashier_allowance_override(
    schedule_id: uuid.UUID,
    payload: CashierAllowanceOverrideRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> ShiftAllowanceOverrideRead:
    require_finance_manager_plus(actor)
    await _validate_cashier_override_request(session, schedule_id, payload)
    override = await upsert_cashier_override(
        session,
        shift_schedule_id=schedule_id,
        business_date=payload.business_date,
        recipient_role=payload.recipient_role,
        recipient_employee_id=payload.recipient_employee_id,
        comment=payload.comment,
        actor=actor,
    )
    return _cashier_override_to_read(override)


@router.patch(
    "/{schedule_id}/cashier-allowance-overrides/{override_id}",
    response_model=ShiftAllowanceOverrideRead,
)
async def patch_cashier_allowance_override(
    schedule_id: uuid.UUID,
    override_id: uuid.UUID,
    payload: CashierAllowanceOverrideRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> ShiftAllowanceOverrideRead:
    require_finance_manager_plus(actor)
    await _validate_cashier_override_request(session, schedule_id, payload)
    override = await patch_cashier_override(
        session,
        shift_schedule_id=schedule_id,
        override_id=override_id,
        business_date=payload.business_date,
        recipient_role=payload.recipient_role,
        recipient_employee_id=payload.recipient_employee_id,
        comment=payload.comment,
        actor=actor,
    )
    return _cashier_override_to_read(override)


@router.delete(
    "/{schedule_id}/cashier-allowance-overrides/{override_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_cashier_allowance_override(
    schedule_id: uuid.UUID,
    override_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> Response:
    require_finance_manager_plus(actor)
    await _ensure_schedule_exists(session, schedule_id)
    await delete_cashier_override(
        session,
        shift_schedule_id=schedule_id,
        override_id=override_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{schedule_id}/cashier-allowance-resolve",
    response_model=AllowanceAssignmentRead,
)
async def get_cashier_allowance_resolve(
    schedule_id: uuid.UUID,
    business_date: date,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> AllowanceAssignmentRead:
    del actor
    schedule = await _get_schedule(session, schedule_id)
    _validate_schedule_business_date(schedule, business_date)
    candidates = await load_cashier_candidates(
        session,
        business_date=business_date,
        use_plan=True,
        use_fact=False,
        shift_schedule_id=schedule_id,
    )
    override = await get_cashier_override(
        session,
        shift_schedule_id=schedule_id,
        business_date=business_date,
    )
    assignment = resolve_cashier_allowance_recipient(
        candidates=candidates,
        manual_override=override,
    )
    return _cashier_assignment_to_read(
        business_date,
        assignment,
        has_manual_override=override is not None,
    )


@router.post("/{schedule_id}/cost-forecast", response_model=PayrollForecastRunRead)
async def post_cost_forecast(
    schedule_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> PayrollForecastRunRead:
    require_finance_manager_plus(actor)
    run = await payroll_forecast_run_service.create_forecast_run(
        session,
        shift_schedule_id=schedule_id,
        actor=actor,
    )
    estimates = await payroll_forecast_run_service.list_estimates(session, run.id)
    return await _payroll_forecast_run_to_read(session, run, estimates=estimates)


@router.get(
    "/{schedule_id}/cost-forecast/latest",
    response_model=PayrollForecastRunRead | None,
)
async def get_latest_cost_forecast(
    schedule_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> PayrollForecastRunRead | None:
    del actor
    await _ensure_schedule_exists(session, schedule_id)
    run = await payroll_forecast_run_service.get_latest_run(session, schedule_id)
    if run is None:
        return None
    estimates = await payroll_forecast_run_service.list_estimates(session, run.id)
    return await _payroll_forecast_run_to_read(session, run, estimates=estimates)


@router.get("/{schedule_id}/cost-forecast/runs", response_model=list[PayrollForecastRunRead])
async def get_cost_forecast_runs(
    schedule_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[PayrollForecastRunRead]:
    del actor
    await _ensure_schedule_exists(session, schedule_id)
    runs = await payroll_forecast_run_service.list_runs(session, schedule_id)
    return await _payroll_forecast_runs_to_read(session, runs)


@router.get(
    "/{schedule_id}/cost-forecast/runs/{run_id}",
    response_model=PayrollForecastRunRead,
)
async def get_cost_forecast_run(
    schedule_id: uuid.UUID,
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> PayrollForecastRunRead:
    del actor
    await _ensure_schedule_exists(session, schedule_id)
    run, estimates = await payroll_forecast_run_service.get_run_with_estimates(session, run_id)
    if run.shift_schedule_id != schedule_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Расчёт не найден",
        )
    return await _payroll_forecast_run_to_read(session, run, estimates=estimates)


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
        payroll_role=payload.payroll_role,
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
        payroll_role=payload.payroll_role,
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


async def _validate_cashier_override_request(
    session: AsyncSession,
    schedule_id: uuid.UUID,
    payload: CashierAllowanceOverrideRequest,
) -> None:
    schedule = await _get_schedule(session, schedule_id)
    _validate_schedule_business_date(schedule, payload.business_date)
    if payload.recipient_role == "none":
        return

    candidates = await load_cashier_candidates(
        session,
        business_date=payload.business_date,
        use_plan=True,
        use_fact=True,
        shift_schedule_id=schedule_id,
    )
    recipient = next(
        (
            candidate
            for candidate in candidates
            if candidate.employee_id == payload.recipient_employee_id
        ),
        None,
    )
    if recipient is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Получатель надбавки должен быть активным кассиром в плане или факте этого дня",
        )
    if payload.recipient_role == "senior" and not recipient.is_senior:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Выбранный сотрудник не является старшим администратором",
        )
    if payload.recipient_role == "deputy_senior" and not recipient.is_deputy_senior:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Выбранный сотрудник не является замом администратора",
        )


def _validate_schedule_business_date(schedule: ShiftSchedule, business_date: date) -> None:
    if not schedule.date_start <= business_date <= schedule.date_end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Дата вне периода графика",
        )


def _cashier_override_to_read(
    override: ShiftAllowanceOverride,
) -> ShiftAllowanceOverrideRead:
    return ShiftAllowanceOverrideRead(
        id=override.id,
        shift_schedule_id=override.shift_schedule_id,
        business_date=override.business_date,
        position=override.position,
        recipient_employee_id=override.recipient_employee_id,
        recipient_role=override.recipient_role,
        comment=override.comment,
        set_by_user_id=override.set_by_user_id,
        set_at=override.set_at,
        created_at=override.created_at,
        updated_at=override.updated_at,
    )


def _cashier_assignment_to_read(
    business_date: date,
    assignment: AllowanceAssignment,
    *,
    has_manual_override: bool,
) -> AllowanceAssignmentRead:
    return AllowanceAssignmentRead(
        business_date=business_date,
        position=CASHIER_POSITION,
        recipient_employee_id=assignment.recipient_employee_id,
        recipient_full_name=recipient_full_name(assignment),
        recipient_role=assignment.recipient_role,
        reason=assignment.reason,
        candidates=[
            AllowanceCandidateRead(
                employee_id=uuid.UUID(str(candidate["employee_id"])),
                full_name=str(candidate["full_name"]),
                is_senior=bool(candidate["is_senior"]),
                is_deputy_senior=bool(candidate["is_deputy_senior"]),
                is_planned=bool(candidate["is_planned"]),
                is_actual=bool(candidate["is_actual"]),
                minutes_worked=int(candidate["minutes_worked"]),
            )
            for candidate in assignment.candidates_snapshot
        ],
        has_manual_override=has_manual_override,
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


async def _payroll_forecast_runs_to_read(
    session: AsyncSession,
    runs: list[PayrollForecastRun],
) -> list[PayrollForecastRunRead]:
    threshold = await payroll_forecast_run_service.get_fot_warning_threshold_pct(session)
    labels = await _user_labels(
        session,
        {run.run_by_user_id for run in runs if run.run_by_user_id is not None},
    )
    return [
        PayrollForecastRunRead(
            id=run.id,
            shift_schedule_id=run.shift_schedule_id,
            run_at=run.run_at,
            run_by_label=labels.get(run.run_by_user_id) if run.run_by_user_id else None,
            status=run.status,
            total_revenue_forecast=run.total_revenue_forecast,
            total_shift_cost_estimate=run.total_shift_cost_estimate,
            fot_to_revenue_pct=run.fot_to_revenue_pct,
            fot_warning_threshold_pct=threshold,
            shifts_total=run.shifts_total,
            shifts_with_warnings=run.shifts_with_warnings,
            estimates=[],
        )
        for run in runs
    ]


async def _payroll_forecast_run_to_read(
    session: AsyncSession,
    run: PayrollForecastRun,
    *,
    estimates: list[ShiftCostEstimate],
) -> PayrollForecastRunRead:
    threshold = await payroll_forecast_run_service.get_fot_warning_threshold_pct(session)
    user_labels = await _user_labels(
        session,
        {run.run_by_user_id} if run.run_by_user_id is not None else set(),
    )
    employee_labels = await _employee_labels(
        session,
        {estimate.employee_id for estimate in estimates},
    )
    return PayrollForecastRunRead(
        id=run.id,
        shift_schedule_id=run.shift_schedule_id,
        run_at=run.run_at,
        run_by_label=user_labels.get(run.run_by_user_id) if run.run_by_user_id else None,
        status=run.status,
        total_revenue_forecast=run.total_revenue_forecast,
        total_shift_cost_estimate=run.total_shift_cost_estimate,
        fot_to_revenue_pct=run.fot_to_revenue_pct,
        fot_warning_threshold_pct=threshold,
        shifts_total=run.shifts_total,
        shifts_with_warnings=run.shifts_with_warnings,
        estimates=[
            ShiftCostEstimateRead(
                id=estimate.id,
                scheduled_shift_id=estimate.scheduled_shift_id,
                business_date=estimate.business_date,
                employee_id=estimate.employee_id,
                employee_full_name=employee_labels.get(estimate.employee_id, ""),
                planned_hours=estimate.planned_hours,
                base_salary_estimate=estimate.base_salary_estimate,
                weekday_premium_estimate=estimate.weekday_premium_estimate,
                allowance_estimate=estimate.allowance_estimate,
                revenue_percent_estimate=estimate.revenue_percent_estimate,
                fund_accrual_estimate=estimate.fund_accrual_estimate,
                total_cost_estimate=estimate.total_cost_estimate,
                quality_status=estimate.quality_status,
                quality_reasons=estimate.quality_reasons,
                breakdown=estimate.breakdown,
            )
            for estimate in estimates
        ],
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


async def _user_labels(
    session: AsyncSession,
    user_ids: set[uuid.UUID],
) -> dict[uuid.UUID, str]:
    if not user_ids:
        return {}
    result = await session.scalars(select(User).where(User.id.in_(user_ids)))
    return {user.id: user.full_name for user in result.all()}


async def _employee_labels(
    session: AsyncSession,
    employee_ids: set[uuid.UUID],
) -> dict[uuid.UUID, str]:
    if not employee_ids:
        return {}
    result = await session.scalars(select(Employee).where(Employee.id.in_(employee_ids)))
    return {employee.id: employee.full_name for employee in result.all()}


async def _attach_plan_fact_source_labels(
    session: AsyncSession,
    summary: plan_fact_service.PlanFactSummary,
) -> None:
    planned_cost = summary.sources.get("planned_cost")
    if not isinstance(planned_cost, dict):
        return
    run_by_user_id = planned_cost.get("run_by_user_id")
    if not isinstance(run_by_user_id, uuid.UUID):
        return
    labels = await _user_labels(session, {run_by_user_id})
    planned_cost["run_by_label"] = labels.get(run_by_user_id)


async def _ensure_schedule_exists(session: AsyncSession, schedule_id: uuid.UUID) -> None:
    if await session.get(ShiftSchedule, schedule_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="График не найден")


async def _get_schedule(session: AsyncSession, schedule_id: uuid.UUID) -> ShiftSchedule:
    schedule = await session.get(ShiftSchedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="График не найден")
    return schedule


def _plan_fact_to_read(summary: plan_fact_service.PlanFactSummary) -> PlanFactSummaryRead:
    return PlanFactSummaryRead(
        schedule=_decimal_data(summary.schedule),
        fact_availability=summary.fact_availability,
        covered_dates=summary.covered_dates,
        planned=_decimal_data(summary.planned),
        actual=_decimal_data(summary.actual) if summary.actual is not None else None,
        deviation=_decimal_data(summary.deviation) if summary.deviation is not None else None,
        warning_threshold_pct=_decimal_to_str(summary.warning_threshold_pct),
        by_date=[
            PlanFactDayRowRead(
                business_date=row.business_date,
                planned_shifts=row.planned_shifts,
                planned_hours=_decimal_to_str(row.planned_hours),
                planned_cost=_optional_decimal_to_str(row.planned_cost),
                planned_revenue=_optional_decimal_to_str(row.planned_revenue),
                actual_shifts=row.actual_shifts,
                actual_hours=_optional_decimal_to_str(row.actual_hours),
                actual_cost=_optional_decimal_to_str(row.actual_cost),
                actual_revenue=_optional_decimal_to_str(row.actual_revenue),
                hours_deviation_pct=_optional_decimal_to_str(row.hours_deviation_pct),
                cost_deviation_pct=_optional_decimal_to_str(row.cost_deviation_pct),
                revenue_deviation_pct=_optional_decimal_to_str(row.revenue_deviation_pct),
                deviation_status=row.deviation_status,
                deviation_flags=row.deviation_flags,
                planned_cashier_allowance=_decimal_data(row.planned_cashier_allowance),
                actual_cashier_allowance=_decimal_data(row.actual_cashier_allowance),
            )
            for row in summary.by_date
        ],
        by_employee=[
            PlanFactEmployeeRowRead(
                employee_id=row.employee_id,
                full_name=row.full_name,
                position=row.position,
                planned_shifts=row.planned_shifts,
                planned_hours=_decimal_to_str(row.planned_hours),
                planned_cost=_optional_decimal_to_str(row.planned_cost),
                actual_shifts=row.actual_shifts,
                actual_hours=_optional_decimal_to_str(row.actual_hours),
                actual_cost=_optional_decimal_to_str(row.actual_cost),
                hours_deviation_pct=_optional_decimal_to_str(row.hours_deviation_pct),
                cost_deviation_pct=_optional_decimal_to_str(row.cost_deviation_pct),
                deviation_status=row.deviation_status,
            )
            for row in summary.by_employee
        ],
        sources=_decimal_data(summary.sources),
    )


def _decimal_data(value: object) -> object:
    if isinstance(value, Decimal):
        return _decimal_to_str(value)
    if isinstance(value, list):
        return [_decimal_data(item) for item in value]
    if isinstance(value, tuple):
        return [_decimal_data(item) for item in value]
    if isinstance(value, dict):
        return {key: _decimal_data(item) for key, item in value.items()}
    return value


def _optional_decimal_to_str(value: Decimal | None) -> str | None:
    return _decimal_to_str(value) if value is not None else None


def _decimal_to_str(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


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

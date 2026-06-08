from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor
from app.models import (
    AppSetting,
    Employee,
    PayrollForecastRun,
    RevenueForecast,
    ScheduledShift,
    ShiftCostEstimate,
    ShiftSchedule,
)
from app.services.employee_assignments import get_assignments
from app.services.employee_effective_events import get_allowances_on_date, get_position_on_date
from app.services.payroll_calculator import (
    CATEGORY_COEFFICIENT_CONFIG_KEY,
    EMPLOYEE_ALLOWANCES_CONFIG_KEY,
    EMPLOYEE_ASSIGNMENTS_CONFIG_KEY,
    EMPLOYEE_POSITIONS_CONFIG_KEY,
    PAYROLL_RATE_CONFIG_KEY,
    load_payroll_rate_versions,
    load_payroll_settings,
)
from app.services.payroll_percent import (
    REVENUE_TIER_CONFIG_KEY,
    load_category_coefficient_versions,
    load_revenue_tier_versions,
)
from app.services.revenue_forecast_service import get_forecasts_in_range
from app.services.seniority_allowance_resolver import (
    CASHIER_ALLOWANCE_ASSIGNMENTS_CONFIG_KEY,
    resolve_cashier_allowance_for_day,
)
from app.services.seniority_allowance_service import (
    SENIORITY_ALLOWANCE_MAP_CONFIG_KEY,
    load_seniority_allowance_maps,
)
from app.services.shift_cost_estimate_service import compute_shift_cost, money

FOT_WARNING_THRESHOLD_KEY = "schedule.fot_warning_threshold_pct"
DEFAULT_FOT_WARNING_THRESHOLD_PCT = Decimal("28.0")
RULE_VERSION = "schedule-cost-preview-v1"


async def create_forecast_run(
    session: AsyncSession,
    *,
    shift_schedule_id: uuid.UUID,
    actor: CurrentActor,
) -> PayrollForecastRun:
    schedule = await session.get(ShiftSchedule, shift_schedule_id)
    if schedule is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="График не найден")

    shift_rows = await _load_shift_rows(session, shift_schedule_id)
    settings = await _load_pricing_settings(
        session,
        shift_schedule_id,
        schedule.date_start,
        schedule.date_end,
        shift_rows,
    )
    forecasts = {
        forecast.business_date: forecast
        for forecast in await get_forecasts_in_range(
            session,
            schedule.date_start,
            schedule.date_end,
        )
    }
    shifts_by_day: dict[date, list[tuple[ScheduledShift, Employee]]] = defaultdict(list)
    for shift, employee in shift_rows:
        shifts_by_day[shift.business_date].append((shift, employee))

    run = PayrollForecastRun(
        id=uuid.uuid4(),
        shift_schedule_id=shift_schedule_id,
        run_by_user_id=actor.user_id,
        rule_version=RULE_VERSION,
        status="draft",
    )
    session.add(run)
    await session.flush()

    estimates: list[ShiftCostEstimate] = []
    total_cost = Decimal("0")
    total_revenue = sum(
        (
            amount
            for amount, _status in (
                _forecast_amount_for_percent(forecast) for forecast in forecasts.values()
            )
            if amount is not None
        ),
        Decimal("0"),
    )
    warnings = 0

    for shift, employee in shift_rows:
        forecast = forecasts.get(shift.business_date)
        daily_revenue, forecast_quality_status = _forecast_amount_for_percent(forecast)

        breakdown = await compute_shift_cost(
            session,
            shift=shift,
            employee=employee,
            settings=settings,
            daily_revenue_forecast=daily_revenue,
            forecast_quality_status=forecast_quality_status,
            same_day_shifts=shifts_by_day[shift.business_date],
        )
        if breakdown.quality_status != "ok":
            warnings += 1
        total_cost += breakdown.total
        estimate = ShiftCostEstimate(
            id=uuid.uuid4(),
            forecast_run_id=run.id,
            scheduled_shift_id=shift.id,
            revenue_forecast_id=forecast.id if forecast is not None else None,
            business_date=shift.business_date,
            employee_id=employee.id,
            planned_hours=breakdown.planned_hours,
            base_salary_estimate=breakdown.base_salary,
            weekday_premium_estimate=breakdown.weekday_premium,
            allowance_estimate=breakdown.allowance,
            revenue_percent_estimate=breakdown.revenue_percent,
            fund_accrual_estimate=breakdown.fund_accrual,
            total_cost_estimate=breakdown.total,
            quality_status=breakdown.quality_status,
            quality_reasons=breakdown.quality_reasons,
            breakdown=breakdown.components,
        )
        session.add(estimate)
        estimates.append(estimate)

    run.total_revenue_forecast = money(total_revenue)
    run.total_shift_cost_estimate = money(total_cost)
    run.fot_to_revenue_pct = (
        (total_cost / total_revenue * Decimal("100")).quantize(Decimal("0.00001"))
        if total_revenue > 0
        else None
    )
    run.shifts_total = len(estimates)
    run.shifts_with_warnings = warnings
    run.status = "completed"

    await session.execute(
        update(PayrollForecastRun)
        .where(
            PayrollForecastRun.shift_schedule_id == shift_schedule_id,
            PayrollForecastRun.id != run.id,
            PayrollForecastRun.status == "completed",
        )
        .values(status="superseded")
    )
    await session.commit()
    await session.refresh(run)
    return run


async def get_latest_run(
    session: AsyncSession,
    shift_schedule_id: uuid.UUID,
) -> PayrollForecastRun | None:
    result = await session.scalars(
        select(PayrollForecastRun)
        .where(
            PayrollForecastRun.shift_schedule_id == shift_schedule_id,
            PayrollForecastRun.status == "completed",
        )
        .order_by(PayrollForecastRun.run_at.desc())
    )
    runs = [
        run
        for run in result.all()
        if run.shift_schedule_id == shift_schedule_id and run.status == "completed"
    ]
    return runs[0] if runs else None


async def list_runs(
    session: AsyncSession,
    shift_schedule_id: uuid.UUID,
) -> list[PayrollForecastRun]:
    result = await session.scalars(
        select(PayrollForecastRun)
        .where(PayrollForecastRun.shift_schedule_id == shift_schedule_id)
        .order_by(PayrollForecastRun.run_at.desc())
    )
    return sorted(
        [run for run in result.all() if run.shift_schedule_id == shift_schedule_id],
        key=lambda run: run.run_at,
        reverse=True,
    )


async def get_run_with_estimates(
    session: AsyncSession,
    run_id: uuid.UUID,
) -> tuple[PayrollForecastRun, list[ShiftCostEstimate]]:
    run = await session.get(PayrollForecastRun, run_id)
    if run is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Расчёт не найден")
    estimates = await list_estimates(session, run.id)
    return run, estimates


async def list_estimates(
    session: AsyncSession,
    run_id: uuid.UUID,
) -> list[ShiftCostEstimate]:
    result = await session.scalars(
        select(ShiftCostEstimate)
        .where(ShiftCostEstimate.forecast_run_id == run_id)
        .order_by(ShiftCostEstimate.business_date, ShiftCostEstimate.employee_id)
    )
    return [estimate for estimate in result.all() if estimate.forecast_run_id == run_id]


async def get_fot_warning_threshold_pct(session: AsyncSession) -> Decimal:
    setting = await session.scalar(
        select(AppSetting).where(AppSetting.key == FOT_WARNING_THRESHOLD_KEY)
    )
    if setting is None:
        return DEFAULT_FOT_WARNING_THRESHOLD_PCT
    value = getattr(setting, "value", DEFAULT_FOT_WARNING_THRESHOLD_PCT)
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - keep bad settings non-blocking for schedule reads
        return DEFAULT_FOT_WARNING_THRESHOLD_PCT


async def _load_shift_rows(
    session: AsyncSession,
    shift_schedule_id: uuid.UUID,
) -> list[tuple[ScheduledShift, Employee]]:
    result = await session.execute(
        select(ScheduledShift, Employee)
        .join(Employee, Employee.id == ScheduledShift.employee_id)
        .where(ScheduledShift.shift_schedule_id == shift_schedule_id)
        .order_by(ScheduledShift.business_date, Employee.full_name)
    )
    return [
        (shift, employee)
        for shift, employee in result.all()
        if shift.shift_schedule_id == shift_schedule_id
    ]


async def _load_pricing_settings(
    session: AsyncSession,
    shift_schedule_id: uuid.UUID,
    date_start: date,
    date_end: date,
    shift_rows: list[tuple[ScheduledShift, Employee]],
) -> dict[str, Any]:
    settings = await load_payroll_settings(session)
    settings[PAYROLL_RATE_CONFIG_KEY] = await load_payroll_rate_versions(
        session,
        date_start,
        date_end,
    )
    settings[REVENUE_TIER_CONFIG_KEY] = await load_revenue_tier_versions(
        session,
        date_start,
        date_end,
    )
    settings[CATEGORY_COEFFICIENT_CONFIG_KEY] = await load_category_coefficient_versions(
        session,
        date_start,
        date_end,
    )
    settings[EMPLOYEE_ASSIGNMENTS_CONFIG_KEY] = await _load_employee_assignments_for_shifts(
        session,
        shift_rows,
    )
    settings[EMPLOYEE_POSITIONS_CONFIG_KEY] = await _load_employee_positions_for_shifts(
        session,
        shift_rows,
    )
    settings[EMPLOYEE_ALLOWANCES_CONFIG_KEY] = await _load_employee_allowances_for_shifts(
        session,
        shift_rows,
    )
    settings[SENIORITY_ALLOWANCE_MAP_CONFIG_KEY] = await load_seniority_allowance_maps(
        session,
        (shift.business_date for shift, _employee in shift_rows),
    )
    settings[
        CASHIER_ALLOWANCE_ASSIGNMENTS_CONFIG_KEY
    ] = await _load_cashier_allowance_assignments_for_schedule(
        session,
        shift_schedule_id,
        shift_rows,
    )
    return settings


async def _load_cashier_allowance_assignments_for_schedule(
    session: AsyncSession,
    shift_schedule_id: uuid.UUID,
    shift_rows: list[tuple[ScheduledShift, Employee]],
) -> dict[date, Any]:
    assignments: dict[date, Any] = {}
    for business_date in sorted({shift.business_date for shift, _employee in shift_rows}):
        assignments[business_date] = await resolve_cashier_allowance_for_day(
            session,
            business_date=business_date,
            shift_schedule_id=shift_schedule_id,
            use_plan=True,
            use_fact=False,
        )
    return assignments


async def _load_employee_assignments_for_shifts(
    session: AsyncSession,
    shift_rows: list[tuple[ScheduledShift, Employee]],
) -> dict[tuple[uuid.UUID, date], list[Any]]:
    assignments_by_day: dict[tuple[uuid.UUID, date], list[Any]] = {}
    for employee_id, business_date in sorted(
        _employee_days(shift_rows),
        key=lambda item: (str(item[0]), item[1]),
    ):
        assignments_by_day[(employee_id, business_date)] = await get_assignments(
            session,
            employee_id,
            business_date,
        )
    return assignments_by_day


async def _load_employee_positions_for_shifts(
    session: AsyncSession,
    shift_rows: list[tuple[ScheduledShift, Employee]],
) -> dict[tuple[uuid.UUID, date], str | None]:
    positions_by_day: dict[tuple[uuid.UUID, date], str | None] = {}
    for employee_id, business_date in sorted(
        _employee_days(shift_rows),
        key=lambda item: (str(item[0]), item[1]),
    ):
        positions_by_day[(employee_id, business_date)] = await get_position_on_date(
            session,
            employee_id,
            business_date,
        )
    return positions_by_day


async def _load_employee_allowances_for_shifts(
    session: AsyncSession,
    shift_rows: list[tuple[ScheduledShift, Employee]],
) -> dict[tuple[uuid.UUID, date], dict[str, bool]]:
    allowances_by_day: dict[tuple[uuid.UUID, date], dict[str, bool]] = {}
    for employee_id, business_date in sorted(
        _employee_days(shift_rows),
        key=lambda item: (str(item[0]), item[1]),
    ):
        allowances_by_day[(employee_id, business_date)] = await get_allowances_on_date(
            session,
            employee_id,
            business_date,
        )
    return allowances_by_day


def _employee_days(
    shift_rows: list[tuple[ScheduledShift, Employee]],
) -> set[tuple[uuid.UUID, date]]:
    return {(shift.employee_id, shift.business_date) for shift, _employee in shift_rows}


def _forecast_amount_for_percent(
    forecast: RevenueForecast | None,
) -> tuple[Decimal | None, str | None]:
    if forecast is None:
        return None, None
    if forecast.quality_status == "requires_review":
        return None, "requires_review"
    if forecast.forecast_amount is None:
        return None, forecast.quality_status
    return Decimal(str(forecast.forecast_amount)), forecast.quality_status

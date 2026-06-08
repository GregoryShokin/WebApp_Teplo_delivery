from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AppSetting,
    Employee,
    PayrollForecastRun,
    PayrollLine,
    PayrollPeriod,
    PayrollRun,
    RevenueForecast,
    ScheduledShift,
    ShiftCostEstimate,
    ShiftLedgerEntry,
    ShiftSchedule,
)
from app.services.iiko_revenue import fetch_daily_revenue
from app.services.seniority_allowance_resolver import (
    AllowanceAssignment,
    assignment_to_dict,
    resolve_cashier_allowance_for_day,
)
from app.services.shift_schedule_service import planned_hours

PLAN_FACT_WARNING_THRESHOLD_KEY = "schedule.plan_fact_warning_threshold_pct"
DEFAULT_PLAN_FACT_WARNING_THRESHOLD_PCT = Decimal("3.0")
MAX_LEDGER_MINUTES = 720
HOURS_QUANT = Decimal("0.01")
MONEY_QUANT = Decimal("0.01")
PCT_QUANT = Decimal("0.01")


@dataclass
class PlanFactDayRow:
    business_date: date
    planned_shifts: int
    planned_hours: Decimal
    planned_cost: Decimal | None
    planned_revenue: Decimal | None
    actual_shifts: int
    actual_hours: Decimal | None
    actual_cost: Decimal | None
    actual_revenue: Decimal | None
    hours_deviation_pct: Decimal | None
    cost_deviation_pct: Decimal | None
    revenue_deviation_pct: Decimal | None
    deviation_status: str
    deviation_flags: list[str]
    planned_cashier_allowance: dict[str, Any] | None
    actual_cashier_allowance: dict[str, Any] | None


@dataclass
class PlanFactEmployeeRow:
    employee_id: uuid.UUID
    full_name: str
    position: str
    planned_shifts: int
    planned_hours: Decimal
    planned_cost: Decimal | None
    actual_shifts: int
    actual_hours: Decimal | None
    actual_cost: Decimal | None
    hours_deviation_pct: Decimal | None
    cost_deviation_pct: Decimal | None
    deviation_status: str


@dataclass
class PlanFactSummary:
    schedule: dict[str, Any]
    fact_availability: str
    covered_dates: list[date]
    planned: dict[str, Any]
    actual: dict[str, Any] | None
    deviation: dict[str, Any] | None
    warning_threshold_pct: Decimal
    by_date: list[PlanFactDayRow]
    by_employee: list[PlanFactEmployeeRow]
    sources: dict[str, Any]


async def compute_plan_fact(
    session: AsyncSession,
    shift_schedule_id: uuid.UUID,
) -> PlanFactSummary:
    schedule = await session.get(ShiftSchedule, shift_schedule_id)
    if schedule is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="График не найден")

    threshold = await get_plan_fact_warning_threshold_pct(session)
    shift_rows = await _load_shift_rows(session, shift_schedule_id)
    shifts = [shift for shift, _employee in shift_rows]

    cost_run = await _load_latest_cost_forecast_run(session, shift_schedule_id)
    cost_estimates = await _load_cost_estimates(session, cost_run.id) if cost_run else []
    forecasts = await _load_revenue_forecasts(session, schedule.date_start, schedule.date_end)
    periods = await _load_payroll_periods(session, schedule.date_start, schedule.date_end)
    payroll_runs_by_period = await _load_latest_payroll_runs_by_period(session, periods)
    payroll_lines = await _load_payroll_lines(session, payroll_runs_by_period.values())

    ledger_from = _ledger_start_date(schedule, periods, payroll_runs_by_period)
    ledger_to = _ledger_end_date(schedule, periods, payroll_runs_by_period)
    ledger_entries = await _load_closed_ledger_entries(session, ledger_from, ledger_to)

    schedule_days = list(_date_range(schedule.date_start, schedule.date_end))
    covered_dates = _covered_dates(schedule_days, periods, payroll_runs_by_period, ledger_entries)
    fact_availability = _fact_availability(schedule_days, covered_dates)

    actual_revenue_by_day = (
        await fetch_daily_revenue(session, schedule.date_start, schedule.date_end)
        if covered_dates
        else {}
    )

    employees = await _load_employees(
        session,
        _employee_ids_from_rows(shift_rows, ledger_entries, payroll_lines),
    )
    forecast_by_day = {forecast.business_date: forecast for forecast in forecasts}
    cost_estimate_by_shift_id = {
        estimate.scheduled_shift_id: estimate for estimate in cost_estimates
    }
    planned_metrics = _build_planned_metrics(
        shifts,
        cost_estimate_by_shift_id,
        forecast_by_day,
        has_cost_forecast=cost_run is not None,
    )
    ledger_metrics = _build_ledger_metrics(
        ledger_entries,
        schedule_days,
        periods,
        payroll_runs_by_period,
    )
    actual_cost = _build_actual_cost_metrics(
        payroll_lines,
        periods,
        payroll_runs_by_period,
        ledger_metrics["minutes_by_period_employee_day"],
        ledger_metrics["minutes_by_period_employee"],
        schedule_days,
    )
    cashier_allowance_by_day = await _build_cashier_allowance_plan_fact(
        session,
        schedule.id,
        schedule_days,
    )

    by_date = [
        _build_day_row(
            day,
            planned_metrics,
            ledger_metrics,
            actual_cost,
            forecast_by_day,
            actual_revenue_by_day,
            threshold,
            cashier_allowance_by_day.get(day),
        )
        for day in schedule_days
    ]
    by_employee = _build_employee_rows(
        planned_metrics,
        ledger_metrics,
        actual_cost,
        employees,
        threshold,
    )
    planned = _build_planned_totals(planned_metrics)
    actual = _build_actual_totals(by_date, actual_cost) if fact_availability != "none" else None
    deviation = _build_summary_deviation(planned, actual) if actual is not None else None

    return PlanFactSummary(
        schedule={
            "id": schedule.id,
            "date_start": schedule.date_start,
            "date_end": schedule.date_end,
            "status": schedule.status,
        },
        fact_availability=fact_availability,
        covered_dates=covered_dates,
        planned=planned,
        actual=actual,
        deviation=deviation,
        warning_threshold_pct=threshold,
        by_date=by_date,
        by_employee=by_employee,
        sources=_build_sources(cost_run, periods, payroll_runs_by_period),
    )


async def get_plan_fact_warning_threshold_pct(session: AsyncSession) -> Decimal:
    setting = await session.scalar(
        select(AppSetting).where(AppSetting.key == PLAN_FACT_WARNING_THRESHOLD_KEY)
    )
    if setting is None:
        return DEFAULT_PLAN_FACT_WARNING_THRESHOLD_PCT
    try:
        return Decimal(str(setting.value))
    except Exception:  # noqa: BLE001 - bad settings should not break analytics reads
        return DEFAULT_PLAN_FACT_WARNING_THRESHOLD_PCT


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


async def _load_latest_cost_forecast_run(
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
    return max(runs, key=lambda run: run.run_at) if runs else None


async def _load_cost_estimates(
    session: AsyncSession,
    forecast_run_id: uuid.UUID,
) -> list[ShiftCostEstimate]:
    result = await session.scalars(
        select(ShiftCostEstimate)
        .where(ShiftCostEstimate.forecast_run_id == forecast_run_id)
        .order_by(ShiftCostEstimate.business_date, ShiftCostEstimate.employee_id)
    )
    return [estimate for estimate in result.all() if estimate.forecast_run_id == forecast_run_id]


async def _load_revenue_forecasts(
    session: AsyncSession,
    date_start: date,
    date_end: date,
) -> list[RevenueForecast]:
    result = await session.scalars(
        select(RevenueForecast)
        .where(
            RevenueForecast.business_date >= date_start,
            RevenueForecast.business_date <= date_end,
        )
        .order_by(RevenueForecast.business_date)
    )
    return [
        forecast for forecast in result.all() if date_start <= forecast.business_date <= date_end
    ]


async def _load_payroll_periods(
    session: AsyncSession,
    date_start: date,
    date_end: date,
) -> list[PayrollPeriod]:
    result = await session.scalars(
        select(PayrollPeriod)
        .where(
            PayrollPeriod.start_date <= date_end,
            PayrollPeriod.end_date >= date_start,
        )
        .order_by(PayrollPeriod.start_date)
    )
    return [
        period
        for period in result.all()
        if period.start_date <= date_end and period.end_date >= date_start
    ]


async def _load_latest_payroll_runs_by_period(
    session: AsyncSession,
    periods: list[PayrollPeriod],
) -> dict[uuid.UUID, PayrollRun]:
    period_ids = {period.id for period in periods}
    if not period_ids:
        return {}

    result = await session.scalars(
        select(PayrollRun)
        .where(
            PayrollRun.period_id.in_(period_ids),
            PayrollRun.status.in_(("completed", "finalized")),
        )
        .order_by(PayrollRun.period_id, PayrollRun.started_at.desc())
    )
    latest: dict[uuid.UUID, PayrollRun] = {}
    for run in result.all():
        if run.period_id not in period_ids or run.status not in {"completed", "finalized"}:
            continue
        existing = latest.get(run.period_id)
        if existing is None or run.started_at > existing.started_at:
            latest[run.period_id] = run
    return latest


async def _load_payroll_lines(
    session: AsyncSession,
    payroll_runs: Any,
) -> list[PayrollLine]:
    run_ids = {run.id for run in payroll_runs}
    if not run_ids:
        return []
    result = await session.scalars(
        select(PayrollLine)
        .where(PayrollLine.run_id.in_(run_ids))
        .order_by(PayrollLine.employee_id, PayrollLine.role)
    )
    return [line for line in result.all() if line.run_id in run_ids]


async def _load_closed_ledger_entries(
    session: AsyncSession,
    date_start: date,
    date_end: date,
) -> list[ShiftLedgerEntry]:
    result = await session.scalars(
        select(ShiftLedgerEntry)
        .where(
            ShiftLedgerEntry.work_date >= date_start,
            ShiftLedgerEntry.work_date <= date_end,
            ShiftLedgerEntry.closed_at.is_not(None),
        )
        .order_by(ShiftLedgerEntry.work_date, ShiftLedgerEntry.employee_id)
    )
    return [
        entry
        for entry in result.all()
        if date_start <= entry.work_date <= date_end and entry.closed_at is not None
    ]


async def _load_employees(
    session: AsyncSession,
    employee_ids: set[uuid.UUID],
) -> dict[uuid.UUID, Employee]:
    if not employee_ids:
        return {}
    result = await session.scalars(select(Employee).where(Employee.id.in_(employee_ids)))
    return {employee.id: employee for employee in result.all() if employee.id in employee_ids}


def _build_planned_metrics(
    shifts: list[ScheduledShift],
    cost_estimate_by_shift_id: dict[uuid.UUID, ShiftCostEstimate],
    forecast_by_day: dict[date, RevenueForecast],
    *,
    has_cost_forecast: bool,
) -> dict[str, Any]:
    shifts_by_day: dict[date, list[ScheduledShift]] = defaultdict(list)
    shifts_by_employee: dict[uuid.UUID, list[ScheduledShift]] = defaultdict(list)
    hours_by_day: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
    hours_by_employee: dict[uuid.UUID, Decimal] = defaultdict(lambda: Decimal("0"))
    cost_by_day: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
    cost_by_employee: dict[uuid.UUID, Decimal] = defaultdict(lambda: Decimal("0"))

    for shift in shifts:
        hours = _quant_hours(planned_hours(shift.planned_start_at, shift.planned_end_at))
        shifts_by_day[shift.business_date].append(shift)
        shifts_by_employee[shift.employee_id].append(shift)
        hours_by_day[shift.business_date] += hours
        hours_by_employee[shift.employee_id] += hours
        estimate = cost_estimate_by_shift_id.get(shift.id)
        if estimate is not None:
            cost = _money(estimate.total_cost_estimate)
            cost_by_day[shift.business_date] += cost
            cost_by_employee[shift.employee_id] += cost

    revenue_by_day = {
        business_date: _optional_money(forecast.forecast_amount)
        for business_date, forecast in forecast_by_day.items()
    }
    return {
        "shifts_by_day": shifts_by_day,
        "shifts_by_employee": shifts_by_employee,
        "hours_by_day": hours_by_day,
        "hours_by_employee": hours_by_employee,
        "cost_by_day": cost_by_day,
        "cost_by_employee": cost_by_employee,
        "revenue_by_day": revenue_by_day,
        "has_cost_forecast": has_cost_forecast,
    }


def _build_ledger_metrics(
    ledger_entries: list[ShiftLedgerEntry],
    schedule_days: list[date],
    periods: list[PayrollPeriod],
    payroll_runs_by_period: dict[uuid.UUID, PayrollRun],
) -> dict[str, Any]:
    schedule_day_set = set(schedule_days)
    covered_periods = [period for period in periods if period.id in payroll_runs_by_period]
    minutes_by_day: dict[date, int] = defaultdict(int)
    employee_ids_by_day: dict[date, set[uuid.UUID]] = defaultdict(set)
    minutes_by_employee: dict[uuid.UUID, int] = defaultdict(int)
    days_by_employee: dict[uuid.UUID, set[date]] = defaultdict(set)
    minutes_by_period_employee: dict[tuple[uuid.UUID, uuid.UUID], int] = defaultdict(int)
    minutes_by_period_employee_day: dict[tuple[uuid.UUID, uuid.UUID, date], int] = defaultdict(int)

    for entry in ledger_entries:
        minutes = _ledger_minutes(entry)
        if minutes <= 0:
            continue
        if entry.work_date in schedule_day_set:
            minutes_by_day[entry.work_date] += minutes
            employee_ids_by_day[entry.work_date].add(entry.employee_id)
            minutes_by_employee[entry.employee_id] += minutes
            days_by_employee[entry.employee_id].add(entry.work_date)
        for period in covered_periods:
            if period.start_date <= entry.work_date <= period.end_date:
                key = (period.id, entry.employee_id)
                minutes_by_period_employee[key] += minutes
                minutes_by_period_employee_day[(period.id, entry.employee_id, entry.work_date)] += (
                    minutes
                )
                break

    return {
        "minutes_by_day": minutes_by_day,
        "employee_ids_by_day": employee_ids_by_day,
        "minutes_by_employee": minutes_by_employee,
        "days_by_employee": days_by_employee,
        "minutes_by_period_employee": minutes_by_period_employee,
        "minutes_by_period_employee_day": minutes_by_period_employee_day,
    }


def _build_actual_cost_metrics(
    payroll_lines: list[PayrollLine],
    periods: list[PayrollPeriod],
    payroll_runs_by_period: dict[uuid.UUID, PayrollRun],
    minutes_by_period_employee_day: dict[tuple[uuid.UUID, uuid.UUID, date], int],
    minutes_by_period_employee: dict[tuple[uuid.UUID, uuid.UUID], int],
    schedule_days: list[date],
) -> dict[str, Any]:
    period_by_id = {period.id: period for period in periods}
    period_by_run_id = {
        run.id: period_by_id[period_id]
        for period_id, run in payroll_runs_by_period.items()
        if period_id in period_by_id
    }
    schedule_day_set = set(schedule_days)
    cost_by_day: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
    cost_by_employee: dict[uuid.UUID, Decimal] = defaultdict(lambda: Decimal("0"))
    has_cost_by_day: set[date] = set()
    has_cost_by_employee: set[uuid.UUID] = set()

    for line in payroll_lines:
        period = period_by_run_id.get(line.run_id)
        if period is None:
            continue
        line_cost = _line_gross_cost(line)
        denominator = minutes_by_period_employee[(period.id, line.employee_id)]
        if denominator <= 0:
            cost_by_employee[line.employee_id] += line_cost
            has_cost_by_employee.add(line.employee_id)
            continue

        for day in _date_range(
            max(period.start_date, min(schedule_days)),
            min(period.end_date, max(schedule_days)),
        ):
            if day not in schedule_day_set:
                continue
            day_minutes = minutes_by_period_employee_day[(period.id, line.employee_id, day)]
            if day_minutes <= 0:
                continue
            day_cost = _money(line_cost * Decimal(day_minutes) / Decimal(denominator))
            cost_by_day[day] += day_cost
            cost_by_employee[line.employee_id] += day_cost
            has_cost_by_day.add(day)
            has_cost_by_employee.add(line.employee_id)

    return {
        "cost_by_day": cost_by_day,
        "cost_by_employee": cost_by_employee,
        "has_cost_by_day": has_cost_by_day,
        "has_cost_by_employee": has_cost_by_employee,
    }


def _build_day_row(
    day: date,
    planned_metrics: dict[str, Any],
    ledger_metrics: dict[str, Any],
    actual_cost: dict[str, Any],
    forecast_by_day: dict[date, RevenueForecast],
    actual_revenue_by_day: dict[date, Decimal],
    threshold: Decimal,
    cashier_allowance: dict[str, AllowanceAssignment] | None,
) -> PlanFactDayRow:
    planned_shifts = len(planned_metrics["shifts_by_day"].get(day, []))
    planned_hours_value = _quant_hours(planned_metrics["hours_by_day"].get(day, Decimal("0")))
    planned_cost = (
        _money(planned_metrics["cost_by_day"].get(day, Decimal("0")))
        if planned_metrics["has_cost_forecast"]
        else None
    )
    planned_revenue = (
        _optional_money(forecast_by_day[day].forecast_amount) if day in forecast_by_day else None
    )
    actual_shifts = len(ledger_metrics["employee_ids_by_day"].get(day, set()))
    actual_hours = _minutes_to_hours_or_none(ledger_metrics["minutes_by_day"].get(day, 0))
    actual_cost_value = (
        _money(actual_cost["cost_by_day"].get(day, Decimal("0")))
        if day in actual_cost["has_cost_by_day"]
        else None
    )
    actual_revenue = _optional_money(actual_revenue_by_day.get(day))
    hours_pct = _deviation_pct(actual_hours, planned_hours_value)
    cost_pct = _deviation_pct(actual_cost_value, planned_cost)
    revenue_pct = _deviation_pct(actual_revenue, planned_revenue)
    planned_cashier_allowance = (
        assignment_to_dict(cashier_allowance["planned"]) if cashier_allowance is not None else None
    )
    actual_cashier_allowance = (
        assignment_to_dict(cashier_allowance["actual"]) if cashier_allowance is not None else None
    )
    deviation_flags: list[str] = []
    if (
        planned_cashier_allowance is not None
        and actual_cashier_allowance is not None
        and planned_cashier_allowance.get("recipient_employee_id")
        != actual_cashier_allowance.get("recipient_employee_id")
    ):
        deviation_flags.append("cashier_allowance_recipient_changed")

    return PlanFactDayRow(
        business_date=day,
        planned_shifts=planned_shifts,
        planned_hours=planned_hours_value,
        planned_cost=planned_cost,
        planned_revenue=planned_revenue,
        actual_shifts=actual_shifts,
        actual_hours=actual_hours,
        actual_cost=actual_cost_value,
        actual_revenue=actual_revenue,
        hours_deviation_pct=hours_pct,
        cost_deviation_pct=cost_pct,
        revenue_deviation_pct=revenue_pct,
        deviation_status=_deviation_status(
            planned_has=_has_plan(
                planned_shifts,
                planned_hours_value,
                planned_cost,
                planned_revenue,
            ),
            actual_has=_has_fact(actual_shifts, actual_hours, actual_cost_value, actual_revenue),
            deviations=[hours_pct, cost_pct],
            threshold=threshold,
        ),
        deviation_flags=deviation_flags,
        planned_cashier_allowance=planned_cashier_allowance,
        actual_cashier_allowance=actual_cashier_allowance,
    )


async def _build_cashier_allowance_plan_fact(
    session: AsyncSession,
    shift_schedule_id: uuid.UUID,
    schedule_days: list[date],
) -> dict[date, dict[str, AllowanceAssignment]]:
    assignments: dict[date, dict[str, AllowanceAssignment]] = {}
    for day in schedule_days:
        planned = await resolve_cashier_allowance_for_day(
            session,
            business_date=day,
            shift_schedule_id=shift_schedule_id,
            use_plan=True,
            use_fact=False,
        )
        actual = await resolve_cashier_allowance_for_day(
            session,
            business_date=day,
            shift_schedule_id=shift_schedule_id,
            use_plan=True,
            use_fact=True,
        )
        assignments[day] = {"planned": planned, "actual": actual}
    return assignments


def _build_employee_rows(
    planned_metrics: dict[str, Any],
    ledger_metrics: dict[str, Any],
    actual_cost: dict[str, Any],
    employees: dict[uuid.UUID, Employee],
    threshold: Decimal,
) -> list[PlanFactEmployeeRow]:
    employee_ids = (
        set(planned_metrics["shifts_by_employee"].keys())
        | set(ledger_metrics["days_by_employee"].keys())
        | set(actual_cost["has_cost_by_employee"])
    )
    rows: list[PlanFactEmployeeRow] = []
    for employee_id in sorted(employee_ids, key=lambda item: _employee_sort_label(item, employees)):
        employee = employees.get(employee_id)
        planned_shifts = len(planned_metrics["shifts_by_employee"].get(employee_id, []))
        planned_hours_value = _quant_hours(
            planned_metrics["hours_by_employee"].get(employee_id, Decimal("0"))
        )
        planned_cost = (
            _money(planned_metrics["cost_by_employee"].get(employee_id, Decimal("0")))
            if planned_metrics["has_cost_forecast"]
            else None
        )
        actual_shifts = len(ledger_metrics["days_by_employee"].get(employee_id, set()))
        actual_hours = _minutes_to_hours_or_none(
            ledger_metrics["minutes_by_employee"].get(employee_id, 0)
        )
        actual_cost_value = (
            _money(actual_cost["cost_by_employee"].get(employee_id, Decimal("0")))
            if employee_id in actual_cost["has_cost_by_employee"]
            else None
        )
        hours_pct = _deviation_pct(actual_hours, planned_hours_value)
        cost_pct = _deviation_pct(actual_cost_value, planned_cost)

        rows.append(
            PlanFactEmployeeRow(
                employee_id=employee_id,
                full_name=employee.full_name if employee is not None else str(employee_id),
                position=employee.position if employee is not None else "",
                planned_shifts=planned_shifts,
                planned_hours=planned_hours_value,
                planned_cost=planned_cost,
                actual_shifts=actual_shifts,
                actual_hours=actual_hours,
                actual_cost=actual_cost_value,
                hours_deviation_pct=hours_pct,
                cost_deviation_pct=cost_pct,
                deviation_status=_deviation_status(
                    planned_has=_has_plan(planned_shifts, planned_hours_value, planned_cost, None),
                    actual_has=_has_fact(actual_shifts, actual_hours, actual_cost_value, None),
                    deviations=[hours_pct, cost_pct],
                    threshold=threshold,
                ),
            )
        )
    return rows


def _build_planned_totals(planned_metrics: dict[str, Any]) -> dict[str, Any]:
    total_shifts = sum(len(shifts) for shifts in planned_metrics["shifts_by_day"].values())
    total_hours = _quant_hours(sum(planned_metrics["hours_by_day"].values(), Decimal("0")))
    total_cost = (
        _money(sum(planned_metrics["cost_by_day"].values(), Decimal("0")))
        if planned_metrics["has_cost_forecast"]
        else None
    )
    total_revenue = _sum_optional(planned_metrics["revenue_by_day"].values())
    return {
        "total_shifts": total_shifts,
        "total_hours": total_hours,
        "total_cost": total_cost,
        "total_revenue": total_revenue,
        "fot_pct": _fot_pct(total_cost, total_revenue),
        "cost_status": "ok" if planned_metrics["has_cost_forecast"] else "no_cost_forecast",
    }


def _build_actual_totals(
    by_date: list[PlanFactDayRow],
    actual_cost: dict[str, Any],
) -> dict[str, Any]:
    total_shifts = sum(row.actual_shifts for row in by_date)
    total_hours = _sum_optional(row.actual_hours for row in by_date)
    total_cost = _sum_optional(actual_cost["cost_by_employee"].values())
    total_revenue = _sum_optional(row.actual_revenue for row in by_date)
    return {
        "total_shifts": total_shifts,
        "total_hours": total_hours,
        "total_cost": total_cost,
        "total_revenue": total_revenue,
        "fot_pct": _fot_pct(total_cost, total_revenue),
    }


def _build_summary_deviation(
    planned: dict[str, Any],
    actual: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if actual is None:
        return None
    planned_fot = planned.get("fot_pct")
    actual_fot = actual.get("fot_pct")
    return {
        "shifts_pct": _deviation_pct(
            Decimal(actual["total_shifts"]),
            Decimal(planned["total_shifts"]),
        ),
        "hours_pct": _deviation_pct(actual.get("total_hours"), planned.get("total_hours")),
        "cost_pct": _deviation_pct(actual.get("total_cost"), planned.get("total_cost")),
        "revenue_pct": _deviation_pct(actual.get("total_revenue"), planned.get("total_revenue")),
        "fot_pct_diff": (
            _quant_pct(actual_fot - planned_fot)
            if actual_fot is not None and planned_fot is not None
            else None
        ),
    }


def _build_sources(
    cost_run: PayrollForecastRun | None,
    periods: list[PayrollPeriod],
    payroll_runs_by_period: dict[uuid.UUID, PayrollRun],
) -> dict[str, Any]:
    period_by_id = {period.id: period for period in periods}
    payroll_sources = []
    for period_id, run in sorted(
        payroll_runs_by_period.items(),
        key=lambda item: period_by_id[item[0]].start_date if item[0] in period_by_id else date.min,
    ):
        period = period_by_id.get(period_id)
        payroll_sources.append(
            {
                "run_id": run.id,
                "status": run.status,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "period_start": period.start_date if period else None,
                "period_end": period.end_date if period else None,
            }
        )
    return {
        "planned_cost": (
            {
                "forecast_run_id": cost_run.id,
                "run_at": cost_run.run_at,
                "run_by_user_id": cost_run.run_by_user_id,
            }
            if cost_run is not None
            else None
        ),
        "actual_cost": payroll_sources,
        "actual_cost_note": (
            "Стоимость дня распределена пропорционально часам внутри payroll-периода."
        ),
    }


def _covered_dates(
    schedule_days: list[date],
    periods: list[PayrollPeriod],
    payroll_runs_by_period: dict[uuid.UUID, PayrollRun],
    ledger_entries: list[ShiftLedgerEntry],
) -> list[date]:
    schedule_day_set = set(schedule_days)
    covered = {
        entry.work_date
        for entry in ledger_entries
        if entry.work_date in schedule_day_set and entry.closed_at is not None
    }
    for period in periods:
        if period.id not in payroll_runs_by_period:
            continue
        for day in _date_range(
            max(period.start_date, min(schedule_days)),
            min(period.end_date, max(schedule_days)),
        ):
            if day in schedule_day_set:
                covered.add(day)
    return sorted(covered)


def _fact_availability(schedule_days: list[date], covered_dates: list[date]) -> str:
    if not covered_dates:
        return "none"
    if len(covered_dates) == len(schedule_days):
        return "full"
    return "partial"


def _ledger_start_date(
    schedule: ShiftSchedule,
    periods: list[PayrollPeriod],
    payroll_runs_by_period: dict[uuid.UUID, PayrollRun],
) -> date:
    covered_starts = [
        period.start_date for period in periods if period.id in payroll_runs_by_period
    ]
    return min([schedule.date_start, *covered_starts])


def _ledger_end_date(
    schedule: ShiftSchedule,
    periods: list[PayrollPeriod],
    payroll_runs_by_period: dict[uuid.UUID, PayrollRun],
) -> date:
    covered_ends = [period.end_date for period in periods if period.id in payroll_runs_by_period]
    return max([schedule.date_end, *covered_ends])


def _employee_ids_from_rows(
    shift_rows: list[tuple[ScheduledShift, Employee]],
    ledger_entries: list[ShiftLedgerEntry],
    payroll_lines: list[PayrollLine],
) -> set[uuid.UUID]:
    return (
        {shift.employee_id for shift, _employee in shift_rows}
        | {employee.id for _shift, employee in shift_rows}
        | {entry.employee_id for entry in ledger_entries}
        | {line.employee_id for line in payroll_lines}
    )


def _employee_sort_label(employee_id: uuid.UUID, employees: dict[uuid.UUID, Employee]) -> str:
    employee = employees.get(employee_id)
    return employee.full_name if employee is not None else str(employee_id)


def _line_gross_cost(line: PayrollLine) -> Decimal:
    return _money(
        _decimal(line.base_pay)
        + _decimal(line.premium)
        + _decimal(line.percent_pay)
        + _decimal(getattr(line, "vacation_pay", 0))
    )


def _ledger_minutes(entry: ShiftLedgerEntry) -> int:
    if entry.closed_at is None:
        return 0
    seconds = (entry.closed_at - entry.opened_at).total_seconds()
    return min(max(int(seconds // 60), 0), MAX_LEDGER_MINUTES)


def _minutes_to_hours_or_none(minutes: int) -> Decimal | None:
    if minutes <= 0:
        return None
    return _quant_hours(Decimal(minutes) / Decimal("60"))


def _deviation_pct(actual: Decimal | None, planned: Decimal | None) -> Decimal | None:
    if actual is None or planned is None or planned <= 0:
        return None
    return _quant_pct((actual - planned) / planned * Decimal("100"))


def _deviation_status(
    *,
    planned_has: bool,
    actual_has: bool,
    deviations: list[Decimal | None],
    threshold: Decimal,
) -> str:
    if not planned_has and not actual_has:
        return "no_data"
    if planned_has and not actual_has:
        return "plan_no_fact"
    if actual_has and not planned_has:
        return "fact_no_plan"
    comparable = [abs(value) for value in deviations if value is not None]
    if not comparable:
        return "within_threshold"
    return "over_threshold" if max(comparable) > threshold else "within_threshold"


def _has_plan(
    shifts: int,
    hours: Decimal | None,
    cost: Decimal | None,
    revenue: Decimal | None,
) -> bool:
    return (
        shifts > 0
        or (hours is not None and hours > 0)
        or (cost is not None and cost > 0)
        or revenue is not None
    )


def _has_fact(
    shifts: int,
    hours: Decimal | None,
    cost: Decimal | None,
    revenue: Decimal | None,
) -> bool:
    return (
        shifts > 0 or (hours is not None and hours > 0) or cost is not None or revenue is not None
    )


def _fot_pct(cost: Decimal | None, revenue: Decimal | None) -> Decimal | None:
    if cost is None or revenue is None or revenue <= 0:
        return None
    return _quant_pct(cost / revenue * Decimal("100"))


def _sum_optional(values: Any) -> Decimal | None:
    total = Decimal("0")
    found = False
    for value in values:
        if value is None:
            continue
        total += _decimal(value)
        found = True
    return _money(total) if found else None


def _optional_money(value: Any) -> Decimal | None:
    if value is None:
        return None
    return _money(value)


def _money(value: Any) -> Decimal:
    return _decimal(value).quantize(MONEY_QUANT)


def _quant_hours(value: Any) -> Decimal:
    return _decimal(value).quantize(HOURS_QUANT)


def _quant_pct(value: Any) -> Decimal:
    return _decimal(value).quantize(PCT_QUANT)


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _date_range(date_start: date, date_end: date):
    cursor = date_start
    while cursor <= date_end:
        yield cursor
        cursor += timedelta(days=1)

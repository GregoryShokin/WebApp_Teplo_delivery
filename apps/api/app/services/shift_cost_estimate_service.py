from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Employee, ScheduledShift
from app.services.employee_assignments import (
    assignment_role_for_payroll_context,
    assignment_role_from_employee,
)
from app.services.payroll_calculator import (
    EMPLOYEE_ALLOWANCES_CONFIG_KEY,
    base_shift_pay,
    category_coeff,
    category_for_payroll_entry,
    fund_accrual_for_day,
    position_for_payroll_entry,
    role_category_rate,
    shift_pay_ratio,
    weekday_premium_for_employee_day,
)
from app.services.payroll_percent import (
    PercentShift,
    compute_daily_percent_pool,
    distribute_percent_pool,
    revenue_tier_rate,
)
from app.services.payroll_calculator import percent_revenue_tiers

MONEY = Decimal("0.01")
HOUR = Decimal(60)
FULL_SHIFT_MINUTES = 720
MAX_REVIEW_MINUTES = 16 * 60

STATION_ROLE_BY_LABEL = {
    "pizza": "pizza",
    "пицца": "pizza",
    "роллы": "sushi",
    "ролл": "sushi",
    "суши": "sushi",
    "sushi": "sushi",
    "shawarma": "shawarma",
    "шаурма": "shawarma",
    "касса": "administrator",
    "administrator": "administrator",
}


@dataclass(slots=True)
class ShiftCostBreakdown:
    planned_hours: Decimal
    base_salary: Decimal
    weekday_premium: Decimal
    allowance: Decimal
    revenue_percent: Decimal
    fund_accrual: Decimal
    total: Decimal
    quality_status: str
    quality_reasons: list[str]
    components: dict[str, Any]


async def compute_shift_cost(
    session: AsyncSession,
    *,
    shift: ScheduledShift,
    employee: Employee,
    settings: Mapping[str, Any],
    daily_revenue_forecast: Decimal | None,
    same_day_shifts: list[tuple[ScheduledShift, Employee]],
    forecast_quality_status: str | None = None,
) -> ShiftCostBreakdown:
    del session
    normalized_settings = normalize_settings(settings)
    planned_minutes = planned_minutes_for_shift(shift)
    planned_hours = (Decimal(planned_minutes) / HOUR).quantize(Decimal("0.01"))
    quality_reasons: list[str] = []

    if planned_minutes > MAX_REVIEW_MINUTES or shift.planned_start_at.date() != shift.planned_end_at.date():
        quality_reasons.append("overnight_shift")

    payroll_role = payroll_role_for_scheduled_shift(shift, employee)
    station_key = station_key_for_payroll(shift.station_code)
    category = ""
    base_salary = Decimal("0")
    rate_exists = False
    if payroll_role is None:
        quality_reasons.append("no_role")
    else:
        category = category_for_payroll_entry(
            normalized_settings,
            employee,
            shift.business_date,
            payroll_role,
            station_key,
        )
        if not category:
            quality_reasons.append("no_category")
        else:
            rate_exists = (
                role_category_rate(
                    normalized_settings,
                    payroll_role,
                    category,
                    shift.business_date,
                    station_key,
                )
                is not None
            )
            if not rate_exists:
                quality_reasons.append("no_rate")
            else:
                base_salary = base_shift_pay(
                    normalized_settings,
                    payroll_role,
                    category,
                    employee,
                    min(planned_minutes, FULL_SHIFT_MINUTES),
                    shift.business_date,
                    station_key,
                )

    weekday_premium = weekday_premium_for_employee_day(
        normalized_settings,
        shift.business_date,
        planned_minutes,
    )
    allowance = Decimal("0")
    revenue_percent, percent_components = revenue_percent_for_shift(
        normalized_settings,
        shift=shift,
        employee=employee,
        category=category,
        daily_revenue_forecast=daily_revenue_forecast,
        forecast_quality_status=forecast_quality_status,
        same_day_shifts=same_day_shifts,
        quality_reasons=quality_reasons,
    )
    fund_accrual = fund_accrual_for_day(
        normalized_settings,
        employee,
        shift.business_date,
        base_salary + weekday_premium,
    )
    total = base_salary + weekday_premium + allowance + revenue_percent
    quality_status = "requires_review" if quality_reasons else "ok"

    allowance_flags = normalized_settings.get(EMPLOYEE_ALLOWANCES_CONFIG_KEY, {}).get(
        (employee.id, shift.business_date)
    )
    if not isinstance(allowance_flags, Mapping):
        allowance_flags = {
            "is_senior": bool(employee.is_senior),
            "is_deputy_senior": bool(employee.is_deputy_senior),
        }

    components = {
        "business_date": shift.business_date.isoformat(),
        "employee_id": str(employee.id),
        "employee_full_name": employee.full_name,
        "role": payroll_role,
        "station": station_key,
        "station_label": shift.station_code,
        "category": category or None,
        "position": position_for_payroll_entry(normalized_settings, employee, shift.business_date),
        "minutes": planned_minutes,
        "planned_hours": money_string(planned_hours),
        "base_salary": money_string(base_salary),
        "weekday_premium": money_string(weekday_premium),
        "allowance": money_string(allowance),
        "allowance_included_in_base": True,
        "allowance_flags": dict(allowance_flags),
        "shift_pay_ratio": money_string(shift_pay_ratio(min(planned_minutes, FULL_SHIFT_MINUTES))),
        "rate_exists": rate_exists,
        "fund_accrual": money_string(fund_accrual),
        "total": money_string(total),
        **percent_components,
    }

    return ShiftCostBreakdown(
        planned_hours=planned_hours,
        base_salary=money(base_salary),
        weekday_premium=money(weekday_premium),
        allowance=money(allowance),
        revenue_percent=money(revenue_percent),
        fund_accrual=money(fund_accrual),
        total=money(total),
        quality_status=quality_status,
        quality_reasons=dedupe(quality_reasons),
        components=components,
    )


def revenue_percent_for_shift(
    settings: Mapping[str, Any],
    *,
    shift: ScheduledShift,
    employee: Employee,
    category: str,
    daily_revenue_forecast: Decimal | None,
    forecast_quality_status: str | None,
    same_day_shifts: list[tuple[ScheduledShift, Employee]],
    quality_reasons: list[str],
) -> tuple[Decimal, dict[str, Any]]:
    if daily_revenue_forecast is None:
        if forecast_quality_status == "requires_review":
            quality_reasons.append("forecast_requires_review")
        else:
            quality_reasons.append("forecast_missing")
        return Decimal("0"), {
            "daily_revenue": None,
            "revenue_rate": None,
            "daily_percent_pool": "0.00",
            "daily_total_weight": "0.00",
            "weight": "0.00",
            "weight_share": "0.00",
            "percent_status": "forecast_missing",
        }

    rate = revenue_tier_rate(daily_revenue_forecast, shift.business_date, percent_revenue_tiers(settings))
    daily_pool = compute_daily_percent_pool(
        daily_revenue_forecast,
        shift.business_date,
        percent_revenue_tiers(settings),
    )
    percent_shifts = percent_shifts_for_day(settings, same_day_shifts)
    distributions = distribute_percent_pool(daily_pool, percent_shifts)
    weights = {item.employee_id: percent_shift_weight(settings, item, shift.business_date) for item in percent_shifts}
    total_weight = sum(weights.values(), Decimal("0"))
    current_weight = weights.get(shift.id, Decimal("0"))
    weight_share = current_weight / total_weight if total_weight > 0 else Decimal("0")
    percent = distributions.get(shift.id, Decimal("0"))
    return percent, {
        "daily_revenue": money_string(daily_revenue_forecast),
        "revenue_rate": money_string(rate or Decimal("0")),
        "daily_percent_pool": money_string(daily_pool),
        "daily_total_weight": money_string(total_weight),
        "weight": money_string(current_weight),
        "weight_share": money_string(weight_share),
        "percent_status": "calculated" if rate is not None and total_weight > 0 else "not_applicable",
    }


def percent_shifts_for_day(
    settings: Mapping[str, Any],
    same_day_shifts: list[tuple[ScheduledShift, Employee]],
) -> list[PercentShift]:
    result: list[PercentShift] = []
    for same_day_shift, same_day_employee in same_day_shifts:
        payroll_role = payroll_role_for_scheduled_shift(same_day_shift, same_day_employee)
        if payroll_role is None:
            category = ""
        else:
            category = category_for_payroll_entry(
                settings,
                same_day_employee,
                same_day_shift.business_date,
                payroll_role,
                station_key_for_payroll(same_day_shift.station_code),
            )
        minutes = min(planned_minutes_for_shift(same_day_shift), FULL_SHIFT_MINUTES)
        coefficient = category_coeff(settings, category, same_day_shift.business_date) if category else Decimal("0")
        result.append(
            PercentShift(
                employee_id=same_day_shift.id,
                category=category,
                hours=Decimal(minutes) / HOUR,
                coefficient=coefficient,
            )
        )
    return result


def percent_shift_weight(settings: Mapping[str, Any], shift: PercentShift, work_date: Any) -> Decimal:
    coefficient = (
        Decimal(str(shift.coefficient))
        if shift.coefficient is not None
        else category_coeff(settings, str(shift.category or ""), work_date)
    )
    hours = min(Decimal(str(shift.hours or 0)), Decimal("12"))
    if coefficient <= 0 or hours <= 0:
        return Decimal("0")
    return coefficient * hours / Decimal("12")


def payroll_role_for_scheduled_shift(
    shift: ScheduledShift,
    employee: Employee,
) -> str | None:
    station_role = STATION_ROLE_BY_LABEL.get(normalized_station(shift.station_code))
    if station_role is not None:
        return station_role
    return (
        assignment_role_for_payroll_context(shift.payroll_role, station_key_for_payroll(shift.station_code))
        or assignment_role_from_employee(employee)
        or shift.payroll_role
    )


def station_key_for_payroll(station: str | None) -> str | None:
    return STATION_ROLE_BY_LABEL.get(normalized_station(station))


def normalized_station(station: str | None) -> str:
    return (station or "").strip().casefold()


def planned_minutes_for_shift(shift: ScheduledShift) -> int:
    return int(round((shift.planned_end_at - shift.planned_start_at).total_seconds() / 60))


def normalize_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(settings)
    normalized.setdefault("payroll.allowances", {})
    normalized.setdefault("payroll.fund_rates_by_tenure", [])
    normalized.setdefault("payroll.weekday_premium", {})
    return normalized


def money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY)


def money_string(value: Any) -> str:
    return str(money(value))


def dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result

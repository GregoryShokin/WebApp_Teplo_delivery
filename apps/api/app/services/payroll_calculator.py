from __future__ import annotations

import json
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting, AttendanceEntry, Employee, PayrollLine, PayrollPeriod

MONEY = Decimal("0.01")
FULL_SHIFT_MINUTES = Decimal(12 * 60)

PAYROLL_SETTING_KEYS = {
    "payroll.role_category_rates",
    "payroll.category_rules",
    "payroll.revenue_percent_tiers",
    "payroll.allowances",
    "payroll.fund_rates_by_tenure",
    "payroll.mock_daily_revenue",
    "payroll.deposit_auto_withholding_enabled",
    "payroll.deposit_fund_payment_date",
}


@dataclass(slots=True)
class PayrollCalculationResult:
    lines: list[PayrollLine]
    blocking_issues: list[dict[str, Any]]
    summary: dict[str, Any]


async def calculate_payroll_lines(
    session: AsyncSession,
    period: PayrollPeriod,
    run_id: uuid.UUID,
    entries: Iterable[AttendanceEntry],
) -> PayrollCalculationResult:
    entries = list(entries)
    employee_ids = {entry.employee_id for entry in entries}
    employees = {
        employee.id: employee
        for employee in (
            await session.scalars(select(Employee).where(Employee.id.in_(employee_ids)))
        ).all()
    }
    settings = await load_payroll_settings(session)
    return calculate_payroll_lines_from_inputs(period, run_id, entries, employees, settings)


async def load_payroll_settings(session: AsyncSession) -> dict[str, Any]:
    result = await session.scalars(
        select(AppSetting).where(AppSetting.key.in_(PAYROLL_SETTING_KEYS))
    )
    return {setting.key: setting.value for setting in result.all()}


def calculate_payroll_lines_from_inputs(
    period: PayrollPeriod,
    run_id: uuid.UUID,
    entries: Iterable[AttendanceEntry],
    employees: Mapping[uuid.UUID, Employee],
    settings: Mapping[str, Any],
) -> PayrollCalculationResult:
    entries = list(entries)
    blocking_issues = validate_calculation_inputs(entries, employees, settings)
    if blocking_issues:
        return PayrollCalculationResult(
            lines=[],
            blocking_issues=blocking_issues,
            summary={"blocking_issue_count": len(blocking_issues)},
        )

    grouped_minutes: dict[tuple[uuid.UUID, str, date], int] = defaultdict(int)
    for entry in entries:
        employee = employees[entry.employee_id]
        role = payroll_role_for_entry(entry, employee)
        grouped_minutes[(entry.employee_id, role, entry.work_date)] += entry.minutes_worked

    daily_coefficients: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
    day_components: dict[tuple[uuid.UUID, str, date], dict[str, Any]] = {}

    for (employee_id, role, work_date), minutes in grouped_minutes.items():
        employee = employees[employee_id]
        category = str(employee.category)
        coeff = category_coeff(settings, category)
        adjusted_coeff = adjusted_shift_coeff(coeff, minutes)
        daily_coefficients[work_date] += adjusted_coeff
        day_components[(employee_id, role, work_date)] = {
            "date": work_date.isoformat(),
            "minutes": minutes,
            "hours": float(Decimal(minutes) / Decimal(60)),
            "role": role,
            "category": category,
            "adjusted_coeff": float(adjusted_coeff),
        }

    line_totals: dict[tuple[uuid.UUID, str], dict[str, Any]] = {}
    for (employee_id, role, work_date), minutes in sorted(
        grouped_minutes.items(), key=lambda item: (item[0][2], item[0][1])
    ):
        employee = employees[employee_id]
        category = str(employee.category)
        base_pay = base_shift_pay(settings, role, category, employee, minutes)
        percent_pay, percent_components = percent_for_day(
            settings,
            work_date,
            day_components[(employee_id, role, work_date)]["adjusted_coeff"],
            daily_coefficients[work_date],
        )
        fund_accrual = fund_for_base_pay(settings, employee, period.end_date, base_pay)
        key = (employee_id, role)
        totals = line_totals.setdefault(
            key,
            {
                "base_pay": Decimal("0"),
                "premium": Decimal("0"),
                "percent_pay": Decimal("0"),
                "fund_accrual": Decimal("0"),
                "deduction": Decimal("0"),
                "days": [],
            },
        )
        totals["base_pay"] += base_pay
        totals["percent_pay"] += percent_pay
        totals["fund_accrual"] += fund_accrual
        day_component = day_components[(employee_id, role, work_date)]
        day_component.update(
            {
                "base_pay": money(base_pay),
                "percent_pay": money(percent_pay),
                "fund_accrual": money(fund_accrual),
                **percent_components,
            }
        )
        totals["days"].append(day_component)

    lines = []
    for (employee_id, role), totals in line_totals.items():
        total_before_deduction = totals["base_pay"] + totals["premium"] + totals["percent_pay"]
        deduction = deposit_withholding(settings, employees[employee_id], total_before_deduction)
        totals["deduction"] = deduction
        total_payable = total_before_deduction - deduction
        lines.append(
            PayrollLine(
                run_id=run_id,
                employee_id=employee_id,
                role=role,
                base_pay=money(totals["base_pay"]),
                premium=money(totals["premium"]),
                percent_pay=money(totals["percent_pay"]),
                fund_accrual=money(totals["fund_accrual"]),
                deduction=money(totals["deduction"]),
                total_payable=money(total_payable),
                components={
                    "days": totals["days"],
                    "deposit_withholding": money(deduction),
                },
            )
        )

    return PayrollCalculationResult(lines=lines, blocking_issues=[], summary=summarize_lines(lines))


def validate_calculation_inputs(
    entries: Iterable[AttendanceEntry],
    employees: Mapping[uuid.UUID, Employee],
    settings: Mapping[str, Any],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    missing_settings = sorted(PAYROLL_SETTING_KEYS - settings.keys())
    for key in missing_settings:
        issues.append({"type": "missing_setting", "setting_key": key})

    for entry in entries:
        employee = employees.get(entry.employee_id)
        if employee is None:
            issues.append(
                {
                    "type": "unknown_employee",
                    "employee_id": str(entry.employee_id),
                    "work_date": entry.work_date.isoformat(),
                }
            )
            continue
        if employee.status == "needs_setup":
            issues.append(needs_setup_issue(employee))
        if entry.quality_status != "ok":
            issues.append(
                {
                    "type": "attendance_quality_review",
                    "employee_id": str(employee.id),
                    "employee_name": employee.full_name,
                    "work_date": entry.work_date.isoformat(),
                    "quality_status": entry.quality_status,
                    "notes": entry.notes,
                }
            )
        if employee.fire_date and entry.work_date > employee.fire_date:
            issues.append(
                {
                    "type": "post_termination_attendance",
                    "employee_id": str(employee.id),
                    "employee_name": employee.full_name,
                    "work_date": entry.work_date.isoformat(),
                    "fire_date": employee.fire_date.isoformat(),
                }
            )
        role = payroll_role_for_entry(entry, employee)
        category = str(employee.category or "")
        if not role:
            issues.append(
                {
                    "type": "missing_payroll_role",
                    "employee_id": str(employee.id),
                    "employee_name": employee.full_name,
                }
            )
        if not category:
            issues.append(needs_setup_issue(employee))
        elif role and not role_category_rate_exists(settings, role, category):
            issues.append(
                {
                    "type": "missing_role_category_rate",
                    "employee_id": str(employee.id),
                    "employee_name": employee.full_name,
                    "role": role,
                    "category": category,
                }
            )
    return deduplicate_issues(issues)


def needs_setup_issue(employee: Employee) -> dict[str, Any]:
    return {
        "type": "needs_setup",
        "employee_id": str(employee.id),
        "employee_iiko_id": employee.iiko_id,
        "employee_name": employee.full_name,
        "cta": {"label": "Исправить", "href": f"/staff?employee={employee.id}"},
    }


def payroll_role_for_entry(entry: AttendanceEntry, employee: Employee) -> str:
    return (entry.role or employee.position or "").strip()


def role_category_rate_exists(settings: Mapping[str, Any], role: str, category: str) -> bool:
    return role_category_rate(settings, role, category) is not None


def role_category_rate(settings: Mapping[str, Any], role: str, category: str) -> Decimal | None:
    rates = settings.get("payroll.role_category_rates")
    if not isinstance(rates, Mapping):
        return None
    value = None
    role_rates = rates.get(role)
    if isinstance(role_rates, Mapping):
        value = role_rates.get(category)
    if value is None:
        value = rates.get(f"{role} / {category}") or rates.get(f"{role}/{category}")
    return decimal_or_none(value)


def category_coeff(settings: Mapping[str, Any], category: str) -> Decimal:
    rules = settings["payroll.category_rules"]
    return decimal(rules[category]["coeff"])


def base_shift_pay(
    settings: Mapping[str, Any],
    role: str,
    category: str,
    employee: Employee,
    minutes: int,
) -> Decimal:
    rate = role_category_rate(settings, role, category) or Decimal("0")
    allowances = settings["payroll.allowances"]
    if employee.is_senior:
        rate += decimal(allowances.get("senior", 0))
    if employee.is_deputy_senior:
        rate += decimal(allowances.get("deputy_senior", 0))
    payable_ratio = min(Decimal(minutes), FULL_SHIFT_MINUTES) / FULL_SHIFT_MINUTES
    return rate * payable_ratio


def adjusted_shift_coeff(coeff: Decimal, minutes: int) -> Decimal:
    if minutes >= int(FULL_SHIFT_MINUTES):
        return coeff
    return coeff * Decimal(minutes) / FULL_SHIFT_MINUTES


def percent_for_day(
    settings: Mapping[str, Any],
    work_date: date,
    adjusted_coeff: float | Decimal,
    total_adjusted_coeff: Decimal,
) -> tuple[Decimal, dict[str, Any]]:
    adjusted_coeff_decimal = decimal(adjusted_coeff)
    revenue = daily_revenue(settings, work_date)
    rate = revenue_percent_rate(settings, revenue)
    if not rate or total_adjusted_coeff <= 0 or adjusted_coeff_decimal <= 0:
        return Decimal("0"), {
            "daily_revenue": money(revenue),
            "revenue_rate": float(rate or 0),
            "daily_total_coeff": float(total_adjusted_coeff),
            "percent_status": "not_applicable",
        }
    value = (revenue * rate / total_adjusted_coeff * adjusted_coeff_decimal).to_integral_value(
        rounding=ROUND_FLOOR
    )
    return value, {
        "daily_revenue": money(revenue),
        "revenue_rate": float(rate),
        "daily_total_coeff": float(total_adjusted_coeff),
        "percent_status": "calculated",
    }


def daily_revenue(settings: Mapping[str, Any], work_date: date) -> Decimal:
    revenue_by_day = settings.get("payroll.mock_daily_revenue", {})
    if not isinstance(revenue_by_day, Mapping):
        return Decimal("0")
    return decimal(revenue_by_day.get(work_date.isoformat(), 0))


def revenue_percent_rate(settings: Mapping[str, Any], revenue: Decimal) -> Decimal | None:
    tiers = settings.get("payroll.revenue_percent_tiers", [])
    best_rate: Decimal | None = None
    for tier in tiers:
        if revenue >= decimal(tier.get("from", 0)):
            best_rate = decimal(tier.get("rate", 0))
    return best_rate


def fund_for_base_pay(
    settings: Mapping[str, Any],
    employee: Employee,
    period_end: date,
    base_pay: Decimal,
) -> Decimal:
    if employee.hire_date is None:
        return Decimal("0")
    tenure_years = Decimal((period_end - employee.hire_date).days) / Decimal("365")
    fund_rate = Decimal("0")
    for tier in settings.get("payroll.fund_rates_by_tenure", []):
        if tenure_years >= decimal(tier.get("min_years", 0)):
            fund_rate = decimal(tier.get("rate", 0))
    return (base_pay * fund_rate).to_integral_value(rounding=ROUND_FLOOR)


def deposit_withholding(
    settings: Mapping[str, Any],
    employee: Employee,
    payable_before_deduction: Decimal,
) -> Decimal:
    if not settings.get("payroll.deposit_auto_withholding_enabled"):
        return Decimal("0")
    category = str(employee.category or "")
    rules = settings["payroll.category_rules"].get(category, {})
    withholding = decimal(rules.get("deposit_withholding", 0))
    return min(withholding, payable_before_deduction)


def summarize_lines(lines: Iterable[PayrollLine]) -> dict[str, Any]:
    lines = list(lines)
    return {
        "line_count": len(lines),
        "base_pay": money(sum((decimal(line.base_pay) for line in lines), Decimal("0"))),
        "premium": money(sum((decimal(line.premium) for line in lines), Decimal("0"))),
        "percent_pay": money(sum((decimal(line.percent_pay) for line in lines), Decimal("0"))),
        "fund_accrual": money(sum((decimal(line.fund_accrual) for line in lines), Decimal("0"))),
        "deduction": money(sum((decimal(line.deduction) for line in lines), Decimal("0"))),
        "total_payable": money(
            sum((decimal(line.total_payable) for line in lines), Decimal("0"))
        ),
    }


def deduplicate_issues(issues: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result = []
    for issue in issues:
        key = json.dumps(issue, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        result.append(issue)
    return result


def decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    return decimal(value)


def money(value: Any) -> float:
    return float(decimal(value).quantize(MONEY))

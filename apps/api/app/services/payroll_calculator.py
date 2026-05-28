from __future__ import annotations

import json
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AppSetting,
    AttendanceEntry,
    Employee,
    PayrollLine,
    PayrollPeriod,
    PayrollRate,
    PayrollRoleCategoryAvailability,
    ShiftLedgerEntry,
)
from app.services.employee_assignments import (
    PAYROLL_ROLE_LABELS,
    assignment_role_for_payroll_context,
    get_assignments,
)
from app.services.payroll_percent import (
    CATEGORY_COEFFICIENT_CONFIG_KEY,
    REVENUE_TIER_CONFIG_KEY,
    PercentShift,
    category_coefficient,
    compute_daily_percent_pool,
    distribute_percent_pool,
    load_category_coefficient_versions,
    load_revenue_tier_versions,
    revenue_tier_rate,
    shift_weight,
)

MONEY = Decimal("0.01")
FULL_SHIFT_MINUTES = Decimal(12 * 60)
CATEGORY_RULE_KEY_BY_APP_CATEGORY = {
    "category_1": "1",
    "category_2": "2",
    "category_3": "3",
    "intern": "4",
    "freelancer": "6",
}

PAYROLL_SETTING_KEYS = {
    "payroll.category_rules",
    "payroll.allowances",
    "payroll.weekday_premium",
    "payroll.fund_rates_by_tenure",
    "payroll.mock_daily_revenue",
    "payroll.deposit_auto_withholding_enabled",
    "payroll.deposit_fund_payment_date",
}
PAYROLL_RATE_CONFIG_KEY = "payroll.role_category_rates_by_date"
EMPLOYEE_ASSIGNMENTS_CONFIG_KEY = "employee.role_assignments_by_date"
SHIFT_LEDGER_CONFIG_KEY = "shift_ledger.entries_by_date"
WEEKDAY_KEYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


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
    settings[PAYROLL_RATE_CONFIG_KEY] = await load_payroll_rate_versions(
        session,
        period.start_date,
        period.end_date,
    )
    settings[REVENUE_TIER_CONFIG_KEY] = await load_revenue_tier_versions(
        session,
        period.start_date,
        period.end_date,
    )
    settings[CATEGORY_COEFFICIENT_CONFIG_KEY] = await load_category_coefficient_versions(
        session,
        period.start_date,
        period.end_date,
    )
    settings[EMPLOYEE_ASSIGNMENTS_CONFIG_KEY] = await load_employee_assignments_for_entries(
        session,
        entries,
    )
    settings[SHIFT_LEDGER_CONFIG_KEY] = await load_shift_ledger_for_entries(session, entries)
    return calculate_payroll_lines_from_inputs(period, run_id, entries, employees, settings)


async def load_payroll_settings(session: AsyncSession) -> dict[str, Any]:
    result = await session.scalars(
        select(AppSetting).where(AppSetting.key.in_(PAYROLL_SETTING_KEYS))
    )
    return {setting.key: setting.value for setting in result.all()}


async def load_payroll_rate_versions(
    session: AsyncSession,
    start_date: date,
    end_date: date,
) -> list[dict[str, Any]]:
    result = await session.scalars(
        select(PayrollRate)
        .join(
            PayrollRoleCategoryAvailability,
            and_(
                PayrollRoleCategoryAvailability.position_group == PayrollRate.position_group,
                PayrollRoleCategoryAvailability.category == PayrollRate.category,
                PayrollRoleCategoryAvailability.is_enabled.is_(True),
            ),
        )
        .where(
            PayrollRate.effective_from <= end_date,
            or_(PayrollRate.effective_to.is_(None), PayrollRate.effective_to > start_date),
            PayrollRate.is_active.is_(True),
            PayrollRate.amount.is_not(None),
        )
    )
    return [
        {
            "position_group": rate.position_group,
            "category": rate.category,
            "station": rate.station,
            "rate_type": rate.rate_type,
            "amount": rate.amount,
            "effective_from": rate.effective_from,
            "effective_to": rate.effective_to,
        }
        for rate in result.all()
    ]


async def load_employee_assignments_for_entries(
    session: AsyncSession,
    entries: Iterable[AttendanceEntry],
) -> dict[tuple[uuid.UUID, date], list[Any]]:
    assignments_by_day: dict[tuple[uuid.UUID, date], list[Any]] = {}
    entry_days = {(entry.employee_id, entry.work_date) for entry in entries}
    for employee_id, work_date in sorted(
        entry_days,
        key=lambda item: (str(item[0]), item[1]),
    ):
        assignments_by_day[(employee_id, work_date)] = await get_assignments(
            session,
            employee_id,
            work_date,
        )
    return assignments_by_day


async def load_shift_ledger_for_entries(
    session: AsyncSession,
    entries: Iterable[AttendanceEntry],
) -> dict[tuple[uuid.UUID, date], ShiftLedgerEntry]:
    entry_days = {(entry.employee_id, entry.work_date) for entry in entries}
    if not entry_days:
        return {}
    employee_ids = {employee_id for employee_id, _work_date in entry_days}
    work_dates = {work_date for _employee_id, work_date in entry_days}
    result = await session.scalars(
        select(ShiftLedgerEntry).where(
            ShiftLedgerEntry.employee_id.in_(employee_ids),
            ShiftLedgerEntry.work_date.in_(work_dates),
        )
    )
    return {(entry.employee_id, entry.work_date): entry for entry in result.all()}


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

    grouped_minutes: dict[tuple[uuid.UUID, str, date, str | None], int] = defaultdict(int)
    group_categories: dict[tuple[uuid.UUID, str, date, str | None], str] = {}
    for entry in entries:
        employee = employees[entry.employee_id]
        role = payroll_role_for_entry(entry, employee, settings)
        station = (entry.station or "").strip() or None
        group_key = (entry.employee_id, role, entry.work_date, station)
        grouped_minutes[group_key] += entry.minutes_worked
        group_categories[group_key] = category_for_payroll_entry(
            settings,
            employee,
            entry.work_date,
            role,
            station,
        )

    day_components: dict[tuple[uuid.UUID, str, date, str | None], dict[str, Any]] = {}
    daily_percent_shifts: dict[date, list[PercentShift]] = defaultdict(list)
    daily_total_coefficients: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))

    for (employee_id, role, work_date, station), minutes in grouped_minutes.items():
        employee = employees[employee_id]
        group_key = (employee_id, role, work_date, station)
        category = group_categories[group_key]
        coeff = category_coeff(settings, category, work_date)
        hours = Decimal(minutes) / Decimal(60)
        percent_shift = PercentShift(
            employee_id=group_key,
            category=category,
            hours=hours,
            coefficient=coeff,
        )
        adjusted_coeff = shift_weight(percent_shift)
        daily_percent_shifts[work_date].append(percent_shift)
        daily_total_coefficients[work_date] += adjusted_coeff
        day_components[group_key] = {
            "date": work_date.isoformat(),
            "minutes": minutes,
            "hours": float(hours),
            "role": role,
            "category": category,
            "station": station,
            "adjusted_coeff": float(adjusted_coeff),
        }

    daily_percent_distributions = {
        work_date: distribute_percent_pool(
            compute_daily_percent_pool(
                daily_revenue(settings, work_date),
                work_date,
                percent_revenue_tiers(settings),
            ),
            shifts,
        )
        for work_date, shifts in daily_percent_shifts.items()
    }
    daily_percent_components = {
        work_date: percent_components_for_day(
            settings,
            work_date,
            daily_total_coefficients[work_date],
        )
        for work_date in daily_percent_shifts
    }

    line_totals: dict[tuple[uuid.UUID, str], dict[str, Any]] = {}
    for (employee_id, role, work_date, station), minutes in sorted(
        grouped_minutes.items(), key=lambda item: (item[0][2], item[0][1], item[0][3] or "")
    ):
        employee = employees[employee_id]
        group_key = (employee_id, role, work_date, station)
        category = group_categories[group_key]
        base_pay = base_shift_pay(settings, role, category, employee, minutes, work_date, station)
        percent_pay = daily_percent_distributions.get(work_date, {}).get(group_key, Decimal("0"))
        percent_components = daily_percent_components.get(work_date, {})
        weekday_premium = weekday_premium_for_day(settings, work_date)
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
        totals["premium"] += weekday_premium
        totals["percent_pay"] += percent_pay
        totals["fund_accrual"] += fund_accrual
        day_component = day_components[group_key]
        day_component.update(
            {
                "base_pay": money(base_pay),
                "weekday_premium": money(weekday_premium),
                "premium": money(weekday_premium),
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
        if employee.status == "requires_setup":
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
        role = payroll_role_for_entry(entry, employee, settings)
        category = category_for_payroll_entry(
            settings,
            employee,
            entry.work_date,
            role,
            entry.station,
        )
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
        elif role and not role_category_rate_exists(
            settings,
            role,
            category,
            entry.work_date,
            entry.station,
        ):
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


def payroll_role_for_entry(
    entry: AttendanceEntry,
    employee: Employee,
    settings: Mapping[str, Any] | None = None,
) -> str:
    if settings is not None:
        ledger_entry = ledger_entry_for_employee_date(settings, employee.id, entry.work_date)
        if ledger_entry is not None:
            return clean_string(getattr(ledger_entry, "payroll_role", None))
    return (entry.role or "").strip()


def category_for_payroll_entry(
    settings: Mapping[str, Any],
    employee: Employee,
    work_date: date,
    role: str | None,
    station: str | None,
) -> str:
    ledger_entry = ledger_entry_for_employee_date(settings, employee.id, work_date)
    if ledger_entry is not None:
        return clean_string(getattr(ledger_entry, "category", None))

    assignments = assignments_for_employee_date(settings, employee.id, work_date)
    if assignments:
        assignment_role = assignment_role_for_payroll_context(role, station)
        if assignment_role:
            for assignment in assignments:
                category = getattr(assignment, "category", None)
                if (
                    getattr(assignment, "payroll_role", None) == assignment_role
                    and category
                ):
                    return str(category)
        primary = next(
            (
                assignment
                for assignment in assignments
                if getattr(assignment, "is_primary", False)
                and getattr(assignment, "category", None)
            ),
            None,
        )
        if primary is not None:
            return str(primary.category)
        first = next(
            (assignment for assignment in assignments if getattr(assignment, "category", None)),
            None,
        )
        if first is not None:
            return str(first.category)
    return str(employee.category or "")


def ledger_entry_for_employee_date(
    settings: Mapping[str, Any],
    employee_id: uuid.UUID,
    work_date: date,
) -> Any | None:
    ledger_by_day = settings.get(SHIFT_LEDGER_CONFIG_KEY)
    if not isinstance(ledger_by_day, Mapping):
        return None
    value = ledger_by_day.get((employee_id, work_date))
    if value is None:
        value = ledger_by_day.get((str(employee_id), work_date.isoformat()))
    return value


def assignments_for_employee_date(
    settings: Mapping[str, Any],
    employee_id: uuid.UUID,
    work_date: date,
) -> list[Any]:
    assignments_by_day = settings.get(EMPLOYEE_ASSIGNMENTS_CONFIG_KEY)
    if not isinstance(assignments_by_day, Mapping):
        return []
    value = assignments_by_day.get((employee_id, work_date))
    if value is None:
        value = assignments_by_day.get((str(employee_id), work_date.isoformat()))
    return list(value) if isinstance(value, Iterable) and not isinstance(value, str | bytes) else []


def role_category_rate_exists(
    settings: Mapping[str, Any],
    role: str,
    category: str,
    work_date: date | None = None,
    station: str | None = None,
) -> bool:
    return role_category_rate(settings, role, category, work_date, station) is not None


def role_category_rate(
    settings: Mapping[str, Any],
    role: str,
    category: str,
    work_date: date | None = None,
    station: str | None = None,
) -> Decimal | None:
    if category_rule_key(category) == "6":
        return None

    versioned_rate = role_category_rate_from_versions(settings, role, category, work_date, station)
    if versioned_rate is not None:
        return versioned_rate

    rates = settings.get("payroll.role_category_rates")
    if not isinstance(rates, Mapping):
        return None
    value = None
    for role_key in role_lookup_keys(role):
        role_rates = rates.get(role_key)
        if isinstance(role_rates, Mapping):
            value = role_rates.get(category)
            if value is None:
                value = role_rates.get(category_rule_key(category))
        if value is not None:
            break
    if value is None:
        normalized_category = category_rule_key(category)
        for role_key in role_lookup_keys(role):
            value = (
                rates.get(f"{role_key} / {category}")
                or rates.get(f"{role_key}/{category}")
                or rates.get(f"{role_key} / {normalized_category}")
                or rates.get(f"{role_key}/{normalized_category}")
            )
            if value is not None:
                break
    return decimal_or_none(value)


def role_category_rate_from_versions(
    settings: Mapping[str, Any],
    role: str,
    category: str,
    work_date: date | None,
    station: str | None,
) -> Decimal | None:
    rates = settings.get(PAYROLL_RATE_CONFIG_KEY)
    if not isinstance(rates, list):
        return None

    candidates: list[tuple[int, date, Decimal]] = []
    normalized_roles = normalized_role_keys(role)
    normalized_categories = normalized_category_keys(category)
    normalized_station = normalized_key(station)

    for item in rates:
        if not isinstance(item, Mapping):
            continue
        if normalized_key(item.get("position_group")) not in normalized_roles:
            continue
        if normalized_key(item.get("category")) not in normalized_categories:
            continue
        if str(item.get("rate_type") or "daily") != "daily":
            continue

        item_station = normalized_key(item.get("station"))
        if item_station and item_station != normalized_station:
            continue
        if item_station and not normalized_station:
            continue

        effective_from = date_or_none(item.get("effective_from"))
        effective_to = date_or_none(item.get("effective_to"))
        if work_date is not None:
            if effective_from is not None and effective_from > work_date:
                continue
            if effective_to is not None and effective_to <= work_date:
                continue

        amount = decimal_or_none(item.get("amount"))
        if amount is None:
            continue
        station_score = 1 if item_station else 0
        candidates.append((station_score, effective_from or date.min, amount))

    if not candidates:
        return None
    candidates.sort(key=lambda candidate: (candidate[0], candidate[1]), reverse=True)
    return candidates[0][2]


def category_coeff(settings: Mapping[str, Any], category: str, work_date: date) -> Decimal:
    coefficients = settings.get(CATEGORY_COEFFICIENT_CONFIG_KEY)
    return category_coefficient(
        category,
        work_date,
        coefficients if isinstance(coefficients, list) else None,
    )


def base_shift_pay(
    settings: Mapping[str, Any],
    role: str,
    category: str,
    employee: Employee,
    minutes: int,
    work_date: date | None = None,
    station: str | None = None,
) -> Decimal:
    rate = role_category_rate(settings, role, category, work_date, station) or Decimal("0")
    allowances = settings["payroll.allowances"]
    if employee.is_senior:
        rate += decimal(allowances.get("senior", 0))
    if employee.is_deputy_senior:
        rate += decimal(allowances.get("deputy_senior", 0))
    payable_ratio = min(Decimal(minutes), FULL_SHIFT_MINUTES) / FULL_SHIFT_MINUTES
    return rate * payable_ratio


def weekday_premium_for_day(settings: Mapping[str, Any], work_date: date) -> Decimal:
    premiums = settings.get("payroll.weekday_premium", {})
    if not isinstance(premiums, Mapping):
        return Decimal("0")
    return decimal(premiums.get(WEEKDAY_KEYS[work_date.weekday()], 0))


def percent_components_for_day(
    settings: Mapping[str, Any],
    work_date: date,
    total_adjusted_coeff: Decimal,
) -> dict[str, Any]:
    revenue = daily_revenue(settings, work_date)
    rate = revenue_percent_rate(settings, revenue, work_date)
    daily_pool = compute_daily_percent_pool(revenue, work_date, percent_revenue_tiers(settings))
    if not rate or total_adjusted_coeff <= 0:
        return {
            "daily_revenue": money(revenue),
            "revenue_rate": float(rate or 0),
            "daily_percent_pool": money(daily_pool),
            "daily_total_coeff": float(total_adjusted_coeff),
            "percent_status": "not_applicable",
        }
    return {
        "daily_revenue": money(revenue),
        "revenue_rate": float(rate),
        "daily_percent_pool": money(daily_pool),
        "daily_total_coeff": float(total_adjusted_coeff),
        "percent_status": "calculated",
    }


def daily_revenue(settings: Mapping[str, Any], work_date: date) -> Decimal:
    revenue_by_day = settings.get("payroll.mock_daily_revenue", {})
    if not isinstance(revenue_by_day, Mapping):
        return Decimal("0")
    return decimal(revenue_by_day.get(work_date.isoformat(), 0))


def revenue_percent_rate(
    settings: Mapping[str, Any],
    revenue: Decimal,
    work_date: date,
) -> Decimal | None:
    return revenue_tier_rate(revenue, work_date, percent_revenue_tiers(settings))


def percent_revenue_tiers(settings: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    tiers = settings.get(REVENUE_TIER_CONFIG_KEY)
    return tiers if isinstance(tiers, list) else None


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
    rules = settings["payroll.category_rules"].get(category_rule_key(category), {})
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
        "total_payable": money(sum((decimal(line.total_payable) for line in lines), Decimal("0"))),
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


def normalized_key(value: Any) -> str:
    return str(value or "").strip().casefold()


def normalized_role_keys(value: Any) -> set[str]:
    return {normalized_key(role) for role in role_lookup_keys(value)}


def role_lookup_keys(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    keys = [text]
    label = PAYROLL_ROLE_LABELS.get(text)
    if label and label not in keys:
        keys.append(label)
    return keys


def clean_string(value: Any) -> str:
    return str(value or "").strip()


def normalized_category_keys(value: Any) -> set[str]:
    text = str(value or "").strip()
    return {normalized_key(text), normalized_key(category_rule_key(text))}


def category_rule_key(value: Any) -> str:
    text = str(value or "").strip()
    return CATEGORY_RULE_KEY_BY_APP_CATEGORY.get(text, text)


def date_or_none(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None

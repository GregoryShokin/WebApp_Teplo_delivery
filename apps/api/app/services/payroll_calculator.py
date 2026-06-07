from __future__ import annotations

import json
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AppSetting,
    AttendanceEntry,
    DepositAccount,
    Employee,
    PayrollAdjustment,
    PayrollLine,
    PayrollPeriod,
    PayrollRate,
    PayrollRoleCategoryAvailability,
    ShiftLedgerEntry,
)
from app.services import vacation_service
from app.services.attendance_loader import PAYROLL_TARGET_POSITIONS
from app.services.employee_assignments import (
    PAYROLL_ROLE_LABELS,
    assignment_role_for_payroll_context,
    get_assignments,
)
from app.services.employee_effective_events import get_allowances_on_date, get_position_on_date
from app.services.payroll_adjustment_service import load_adjustments_for_period
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
from app.services.seniority_allowance_resolver import (
    CASHIER_ALLOWANCE_ASSIGNMENTS_CONFIG_KEY,
    CASHIER_POSITION,
    latest_published_schedule_id_covering,
    resolve_cashier_allowance_for_day,
)
from app.services.seniority_allowance_service import (
    SENIORITY_ALLOWANCE_MAP_CONFIG_KEY,
    allowance_amount_from_settings,
    load_seniority_allowance_maps,
)

MONEY = Decimal("0.01")
FULL_SHIFT_MINUTES = Decimal(12 * 60)
DEFAULT_WEEKDAY_PREMIUM_AMOUNT = Decimal("200")
DEFAULT_WEEKDAY_PREMIUM_THRESHOLD_HOURS = Decimal("8")
WEEKDAY_PREMIUM_WEEKDAYS = {4, 5}
PAYROLL_NDFL_CONFIG_KEY = "payroll.ndfl"
DEFAULT_NDFL_RATE = Decimal("0.13")
NDFL_ROUNDING = "round_half_up_to_kopeck"
CATEGORY_RULE_KEY_BY_APP_CATEGORY = {
    "category_1": "1",
    "category_2": "2",
    "category_3": "3",
    "category_4": "4",
    "intern": "4",
    "freelancer": "6",
}

PAYROLL_SETTING_KEYS = {
    "payroll.category_rules",
    "payroll.weekday_premium",
    "payroll.fund_rates_by_tenure",
    "payroll.daily_revenue_by_date",
    "payroll.mock_daily_revenue",
    "payroll.deposit_auto_withholding_enabled",
    "payroll.deposit_fund_payment_date",
    "payroll.deposit_write_offs",
    PAYROLL_NDFL_CONFIG_KEY,
}
OPTIONAL_PAYROLL_SETTING_KEYS = {
    "payroll.daily_revenue_by_date",
    "payroll.deposit_write_offs",
    PAYROLL_NDFL_CONFIG_KEY,
}
DAILY_REVENUE_CONFIG_KEY = "payroll.daily_revenue_by_date"
PAYROLL_RATE_CONFIG_KEY = "payroll.role_category_rates_by_date"
PAYROLL_RATE_SNAPSHOT_SUMMARY_KEY = "payroll_rate_snapshot"
EMPLOYEE_ASSIGNMENTS_CONFIG_KEY = "employee.role_assignments_by_date"
EMPLOYEE_POSITIONS_CONFIG_KEY = "employee.positions_by_date"
EMPLOYEE_ALLOWANCES_CONFIG_KEY = "employee.allowances_by_date"
SHIFT_LEDGER_CONFIG_KEY = "shift_ledger.entries_by_date"
PAYROLL_ADJUSTMENTS_CONFIG_KEY = "payroll.adjustments_by_date"
VACATION_DAYS_CONFIG_KEY = "vacation.days_by_employee_date"
VACATION_DAILY_AMOUNT_CONFIG_KEY = vacation_service.VACATION_DAILY_AMOUNT_KEY
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


_MISSING_CONFIG_VALUE = object()


async def calculate_payroll_lines(
    session: AsyncSession,
    period: PayrollPeriod,
    run_id: uuid.UUID,
    entries: Iterable[AttendanceEntry],
    line_deposit_overrides: Mapping[tuple[uuid.UUID, str], Mapping[str, Any]] | None = None,
    rate_versions_override: Iterable[Mapping[str, Any]] | None = None,
) -> PayrollCalculationResult:
    entries = list(entries)
    vacation_days = await vacation_service.vacation_days_for_payroll_period(
        session,
        period_start=period.start_date,
        period_end=period.end_date,
    )
    employee_ids = {entry.employee_id for entry in entries} | {
        employee_id for employee_id, _work_date in vacation_days
    }
    employees = {
        employee.id: employee
        for employee in (
            await session.scalars(select(Employee).where(Employee.id.in_(employee_ids)))
        ).all()
    }
    settings = await load_payroll_settings(session)
    settings[VACATION_DAYS_CONFIG_KEY] = vacation_days
    settings[VACATION_DAILY_AMOUNT_CONFIG_KEY] = await vacation_service.vacation_daily_amount(
        session
    )
    rate_versions = (
        list(rate_versions_override)
        if rate_versions_override is not None
        else await load_payroll_rate_versions(
            session,
            period.start_date,
            period.end_date,
        )
    )
    settings[PAYROLL_RATE_CONFIG_KEY] = rate_versions
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
        extra_employee_days=vacation_days,
    )
    settings[EMPLOYEE_POSITIONS_CONFIG_KEY] = await load_employee_positions_for_entries(
        session,
        entries,
        extra_employee_days=vacation_days,
    )
    settings[EMPLOYEE_ALLOWANCES_CONFIG_KEY] = await load_employee_allowances_for_entries(
        session,
        entries,
        extra_employee_days=vacation_days,
    )
    settings[SENIORITY_ALLOWANCE_MAP_CONFIG_KEY] = await load_seniority_allowance_maps(
        session,
        (entry.work_date for entry in entries),
    )
    settings[
        CASHIER_ALLOWANCE_ASSIGNMENTS_CONFIG_KEY
    ] = await load_cashier_allowance_assignments_for_entries(session, entries)
    settings[SHIFT_LEDGER_CONFIG_KEY] = await load_shift_ledger_for_entries(session, entries)
    settings[PAYROLL_ADJUSTMENTS_CONFIG_KEY] = await load_adjustments_for_period(
        session,
        employee_ids=employee_ids,
        period_start=period.start_date,
        period_end=period.end_date,
    )
    deposit_balances = await load_deposit_balances_for_employees(session, employee_ids)
    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        entries,
        employees,
        settings,
        deposit_balances=deposit_balances,
        line_deposit_overrides=line_deposit_overrides,
    )
    result.summary = result.summary | {
        PAYROLL_RATE_SNAPSHOT_SUMMARY_KEY: payroll_rate_snapshot_payload(
            rate_versions,
            locked=False,
        )
    }
    return result


async def load_payroll_settings(session: AsyncSession) -> dict[str, Any]:
    result = await session.scalars(
        select(AppSetting).where(AppSetting.key.in_(PAYROLL_SETTING_KEYS))
    )
    settings: dict[str, Any] = {}
    for setting in result.all():
        if setting.key == "payroll.weekday_premium":
            settings[setting.key] = normalize_weekday_premium_setting(
                setting.value,
                setting.widget_options,
            )
        else:
            settings[setting.key] = setting.value
    return settings


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
            "id": str(rate.id),
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
    extra_employee_days: Iterable[tuple[uuid.UUID, date]] | None = None,
) -> dict[tuple[uuid.UUID, date], list[Any]]:
    assignments_by_day: dict[tuple[uuid.UUID, date], list[Any]] = {}
    entry_days = {(entry.employee_id, entry.work_date) for entry in entries}
    entry_days |= set(extra_employee_days or [])
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


async def load_employee_positions_for_entries(
    session: AsyncSession,
    entries: Iterable[AttendanceEntry],
    extra_employee_days: Iterable[tuple[uuid.UUID, date]] | None = None,
) -> dict[tuple[uuid.UUID, date], str | None]:
    positions_by_day: dict[tuple[uuid.UUID, date], str | None] = {}
    entry_days = {(entry.employee_id, entry.work_date) for entry in entries}
    entry_days |= set(extra_employee_days or [])
    for employee_id, work_date in sorted(
        entry_days,
        key=lambda item: (str(item[0]), item[1]),
    ):
        positions_by_day[(employee_id, work_date)] = await get_position_on_date(
            session,
            employee_id,
            work_date,
        )
    return positions_by_day


async def load_employee_allowances_for_entries(
    session: AsyncSession,
    entries: Iterable[AttendanceEntry],
    extra_employee_days: Iterable[tuple[uuid.UUID, date]] | None = None,
) -> dict[tuple[uuid.UUID, date], dict[str, bool]]:
    allowances_by_day: dict[tuple[uuid.UUID, date], dict[str, bool]] = {}
    entry_days = {(entry.employee_id, entry.work_date) for entry in entries}
    entry_days |= set(extra_employee_days or [])
    for employee_id, work_date in sorted(
        entry_days,
        key=lambda item: (str(item[0]), item[1]),
    ):
        allowances_by_day[(employee_id, work_date)] = await get_allowances_on_date(
            session,
            employee_id,
            work_date,
        )
    return allowances_by_day


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


async def load_cashier_allowance_assignments_for_entries(
    session: AsyncSession,
    entries: Iterable[AttendanceEntry],
) -> dict[date, Any]:
    assignments: dict[date, Any] = {}
    work_dates = sorted({entry.work_date for entry in entries})
    for work_date in work_dates:
        shift_schedule_id = await latest_published_schedule_id_covering(session, work_date)
        assignments[work_date] = await resolve_cashier_allowance_for_day(
            session,
            business_date=work_date,
            shift_schedule_id=shift_schedule_id,
            use_plan=True,
            use_fact=True,
        )
    return assignments


async def load_deposit_balances_for_employees(
    session: AsyncSession,
    employee_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, Decimal]:
    employee_ids = set(employee_ids)
    if not employee_ids:
        return {}
    result = await session.scalars(
        select(DepositAccount).where(DepositAccount.employee_id.in_(employee_ids))
    )
    return {account.employee_id: decimal(account.balance) for account in result.all()}


def calculate_payroll_lines_from_inputs(
    period: PayrollPeriod,
    run_id: uuid.UUID,
    entries: Iterable[AttendanceEntry],
    employees: Mapping[uuid.UUID, Employee],
    settings: Mapping[str, Any],
    deposit_balances: Mapping[uuid.UUID, Decimal] | None = None,
    line_deposit_overrides: Mapping[tuple[uuid.UUID, str], Mapping[str, Any]] | None = None,
) -> PayrollCalculationResult:
    entries = list(entries)
    vacation_days = vacation_days_from_settings(settings)
    blocking_issues = validate_calculation_inputs(entries, employees, settings)
    if blocking_issues:
        return PayrollCalculationResult(
            lines=[],
            blocking_issues=blocking_issues,
            summary={"blocking_issue_count": len(blocking_issues)},
        )

    grouped_minutes: dict[tuple[uuid.UUID, str, date, str | None], int] = defaultdict(int)
    employee_day_minutes: dict[tuple[uuid.UUID, date], int] = defaultdict(int)
    group_categories: dict[tuple[uuid.UUID, str, date, str | None], str] = {}
    vacation_group_days: set[tuple[uuid.UUID, date]] = set()
    for entry in entries:
        employee = employees[entry.employee_id]
        employee_day_key = (entry.employee_id, entry.work_date)
        if employee_day_key in vacation_days:
            role = vacation_role_for_employee_day(settings, employee, entry.work_date)
            station = None
            group_key = (entry.employee_id, role, entry.work_date, station)
            grouped_minutes[group_key] = 0
            employee_day_minutes[employee_day_key] = 0
            vacation_group_days.add(employee_day_key)
        else:
            role = payroll_role_for_entry(entry, employee, settings)
            station = (entry.station or "").strip() or None
            group_key = (entry.employee_id, role, entry.work_date, station)
            grouped_minutes[group_key] += entry.minutes_worked
            employee_day_minutes[employee_day_key] += entry.minutes_worked
        group_categories[group_key] = category_for_payroll_entry(
            settings,
            employee,
            entry.work_date,
            role,
            station,
        )

    for employee_id, work_date in sorted(vacation_days, key=lambda item: (str(item[0]), item[1])):
        if (employee_id, work_date) in vacation_group_days:
            continue
        employee = employees.get(employee_id)
        if employee is None:
            continue
        role = vacation_role_for_employee_day(settings, employee, work_date)
        station = None
        group_key = (employee_id, role, work_date, station)
        grouped_minutes[group_key] = 0
        employee_day_minutes[(employee_id, work_date)] = 0
        group_categories[group_key] = category_for_payroll_entry(
            settings,
            employee,
            work_date,
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
        position = position_for_payroll_entry(settings, employee, work_date)
        is_vacation_day = (employee_id, work_date) in vacation_days
        is_substitute = is_substitute_payroll_entry(
            settings,
            employee,
            work_date,
            role,
            station,
        )
        coeff = Decimal("0") if is_vacation_day else category_coeff(settings, category, work_date)
        hours = Decimal(minutes) / Decimal(60)
        percent_shift = PercentShift(
            employee_id=group_key,
            category=category,
            hours=hours,
            coefficient=coeff,
        )
        adjusted_coeff = shift_weight(percent_shift)
        if not is_vacation_day:
            daily_percent_shifts[work_date].append(percent_shift)
            daily_total_coefficients[work_date] += adjusted_coeff
        day_components[group_key] = {
            "date": work_date.isoformat(),
            "minutes": 0 if is_vacation_day else minutes,
            "hours": 0 if is_vacation_day else float(hours),
            "role": role,
            "category": category,
            "position": position,
            "station": station,
            "is_substitute": is_substitute,
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
    premium_applied_employee_days: set[tuple[uuid.UUID, date]] = set()
    for (employee_id, role, work_date, station), minutes in sorted(
        grouped_minutes.items(), key=lambda item: (item[0][2], item[0][1], item[0][3] or "")
    ):
        employee = employees[employee_id]
        group_key = (employee_id, role, work_date, station)
        category = group_categories[group_key]
        employee_day_key = (employee_id, work_date)
        is_vacation_day = employee_day_key in vacation_days
        is_substitute = is_substitute_payroll_entry(
            settings,
            employee,
            work_date,
            role,
            station,
        )
        if is_vacation_day:
            rate_details = None
            base_pay = Decimal("0")
            seniority_allowance_pay = Decimal("0")
            seniority_allowance_metadata = {}
            percent_pay = Decimal("0")
            percent_components = {"percent_status": "vacation"}
            weekday_premium = Decimal("0")
            vacation_pay = vacation_daily_amount_from_settings(settings)
            base_pay_with_premium = Decimal("0")
            fund_excluded = False
            fund_rate = Decimal("0")
            fund_accrual = Decimal("0")
        else:
            base_pay = base_shift_pay(
                settings,
                role,
                category,
                employee,
                minutes,
                work_date,
                station,
            )
            if is_substitute:
                seniority_allowance_pay = Decimal("0")
                seniority_allowance_metadata = {"seniority_allowance_skipped_reason": "substitute"}
            else:
                seniority_allowance_pay, seniority_allowance_metadata = (
                    seniority_allowance_details_for_payroll_entry(
                        settings,
                        employee,
                        minutes,
                        work_date,
                    )
                )
            percent_pay = daily_percent_distributions.get(work_date, {}).get(
                group_key, Decimal("0")
            )
            percent_components = daily_percent_components.get(work_date, {})
            weekday_premium = Decimal("0")
            if employee_day_key not in premium_applied_employee_days:
                weekday_premium = weekday_premium_for_employee_day(
                    settings,
                    work_date,
                    employee_day_minutes[employee_day_key],
                )
                premium_applied_employee_days.add(employee_day_key)
            vacation_pay = Decimal("0")
            base_pay_with_premium = base_pay + seniority_allowance_pay + weekday_premium
            fund_excluded = is_fund_currently_excluded(employee, work_date)
            fund_rate = _fund_rate_for_months(settings, tenure_months_on(employee, work_date))
            fund_accrual = (
                Decimal("0")
                if is_substitute
                else fund_accrual_for_day(
                    settings,
                    employee,
                    work_date,
                    base_pay_with_premium,
                )
            )
            rate_details = role_category_rate_details(
                settings,
                role,
                category,
                work_date,
                station,
            )
        key = (employee_id, role)
        totals = line_totals.setdefault(
            key,
            {
                "base_pay": Decimal("0"),
                "premium": Decimal("0"),
                "percent_pay": Decimal("0"),
                "vacation_pay": Decimal("0"),
                "fund_accrual": Decimal("0"),
                "deduction": Decimal("0"),
                "manual_deduction": Decimal("0"),
                "days": [],
                "adjustments": {
                    "bonuses": [],
                    "penalties": [],
                    "primary_role_chosen": None,
                },
            },
        )
        totals["base_pay"] += base_pay_with_premium
        totals["percent_pay"] += percent_pay
        totals["vacation_pay"] += vacation_pay
        totals["fund_accrual"] += fund_accrual
        day_component = day_components[group_key]
        day_component.update(
            {
                "kind": "vacation" if is_vacation_day else "shift",
                "base_pay": money(base_pay_with_premium),
                "base_pay_shift": money(base_pay),
                "base_rate": money(rate_details["amount"]) if rate_details else 0,
                "payroll_rate": payroll_rate_component(rate_details),
                "seniority_allowance_pay": money(seniority_allowance_pay),
                "weekday_premium": money(weekday_premium),
                "percent_pay": money(percent_pay),
                "vacation_pay": money(vacation_pay),
                "fund_rate_percent": float((fund_rate * Decimal("100")).quantize(Decimal("0.001"))),
                "fund_accrual": money(fund_accrual),
                "is_substitute": is_substitute,
                **percent_components,
                **seniority_allowance_metadata,
            }
        )
        if fund_excluded:
            day_component["fund_excluded"] = True
        totals["days"].append(day_component)

    apply_adjustments_to_line_totals(line_totals, settings, period)

    lines = []
    running_deposit_balances = {
        employee_id: decimal(balance) for employee_id, balance in (deposit_balances or {}).items()
    }
    deposit_overrides = line_deposit_overrides or {}
    for (employee_id, role), totals in line_totals.items():
        total_before_deduction = (
            totals["base_pay"] + totals["premium"] + totals["percent_pay"] + totals["vacation_pay"]
        )
        manual_deduction = totals.get("manual_deduction", Decimal("0"))
        ndfl = calculate_ndfl_for_line(settings, employees[employee_id], role, totals)
        totals["ndfl_withheld"] = ndfl["withheld"]
        totals["ndfl_taxable_base"] = ndfl["taxable_base"]
        totals["ndfl_components"] = ndfl["components"]
        apply_ndfl_to_day_components(totals, ndfl)
        current_deposit_balance = running_deposit_balances.get(employee_id, Decimal("0"))
        deposit_base = max(
            total_before_deduction - manual_deduction - ndfl["withheld"],
            Decimal("0"),
        )
        line_override = line_deposit_override(deposit_overrides, employee_id, role)
        deposit_excluded_for_run = bool(line_override.get("deposit_excluded_for_run", False))
        deposit_exclusion_reason = optional_text(line_override.get("deposit_exclusion_reason"))
        is_substitute_line = any(bool(day.get("is_substitute")) for day in totals.get("days", []))
        if deposit_excluded_for_run or is_substitute_line:
            deduction = Decimal("0")
        else:
            deduction = deposit_withholding(
                settings,
                employees[employee_id],
                deposit_base,
                current_balance=current_deposit_balance,
            )
        running_deposit_balances[employee_id] = current_deposit_balance + deduction
        totals["deposit_withholding"] = deduction
        totals["deduction"] = manual_deduction + deduction
        total_payable = total_before_deduction - ndfl["withheld"] - deduction - manual_deduction
        total_payable = max(total_payable, Decimal("0"))
        lines.append(
            PayrollLine(
                run_id=run_id,
                employee_id=employee_id,
                role=role,
                base_pay=money(totals["base_pay"]),
                premium=money(totals["premium"]),
                percent_pay=money(totals["percent_pay"]),
                vacation_pay=money(totals["vacation_pay"]),
                ndfl_withheld=ndfl["withheld"],
                fund_accrual=money(totals["fund_accrual"]),
                deduction=money(totals["deduction"]),
                total_payable=money(total_payable),
                deposit_excluded_for_run=deposit_excluded_for_run,
                deposit_exclusion_reason=deposit_exclusion_reason,
                components={
                    "days": totals["days"],
                    "deposit_withholding": money(deduction),
                    "ndfl_withheld": money(ndfl["withheld"]),
                    "ndfl": ndfl["components"],
                    "adjustments": totals["adjustments"],
                },
            )
        )

    return PayrollCalculationResult(lines=lines, blocking_issues=[], summary=summarize_lines(lines))


def calculate_ndfl_for_line(
    settings: Mapping[str, Any],
    employee: Employee,
    role: str,
    totals: Mapping[str, Any],
) -> dict[str, Any]:
    config = ndfl_config(settings)
    taxable_base, components = ndfl_taxable_base_components(config, totals)
    applies = ndfl_applies_to_line(config, employee, role, totals)
    rate = ndfl_rate(config)
    withheld = (
        ndfl_money(taxable_base * rate)
        if applies and taxable_base > 0 and rate > 0
        else Decimal("0")
    )
    gross_accruals = (
        decimal(totals.get("base_pay"))
        + decimal(totals.get("premium"))
        + decimal(totals.get("percent_pay"))
        + decimal(totals.get("vacation_pay"))
    )
    withheld = min(withheld, max(gross_accruals, Decimal("0")))
    return {
        "withheld": withheld,
        "taxable_base": taxable_base if applies else Decimal("0"),
        "components": {
            "enabled": bool(config.get("enabled", False)),
            "applied": applies,
            "rate": str(rate),
            "base": money_string(taxable_base if applies else Decimal("0")),
            "withheld": money_string(withheld),
            "rounding": NDFL_ROUNDING,
            "base_config": ndfl_base_config_payload(config),
            "base_components": components,
        },
    }


def ndfl_config(settings: Mapping[str, Any]) -> Mapping[str, Any]:
    value = settings.get(PAYROLL_NDFL_CONFIG_KEY)
    return value if isinstance(value, Mapping) else {}


def ndfl_applies_to_line(
    config: Mapping[str, Any],
    employee: Employee,
    role: str,
    totals: Mapping[str, Any],
) -> bool:
    if not bool(config.get("enabled", False)):
        return False

    employee_id = str(employee.id)
    role_keys = normalized_role_keys(role)
    category_keys = ndfl_line_category_keys(employee, totals)

    if employee_id in string_config_values(config.get("excluded_employee_ids")):
        return False
    if role_keys & normalized_config_values(config.get("excluded_roles")):
        return False
    if category_keys & normalized_category_config_values(config.get("excluded_categories")):
        return False

    if employee_id in string_config_values(config.get("employee_ids")):
        return True
    if role_keys & normalized_config_values(config.get("roles")):
        return True
    if category_keys & normalized_category_config_values(config.get("categories")):
        return True
    return bool(config.get("default_employee_enabled", config.get("default_enabled", False)))


def ndfl_line_category_keys(employee: Employee, totals: Mapping[str, Any]) -> set[str]:
    result = normalized_category_keys(getattr(employee, "category", None))
    days = totals.get("days")
    if isinstance(days, Iterable) and not isinstance(days, str | bytes):
        for day in days:
            if isinstance(day, Mapping):
                result.update(normalized_category_keys(day.get("category")))
    return {key for key in result if key}


def ndfl_rate(config: Mapping[str, Any]) -> Decimal:
    value = config.get("rate", DEFAULT_NDFL_RATE)
    try:
        if "rate_percent" in config:
            value = decimal(config.get("rate_percent")) / Decimal("100")
        rate = decimal(value)
    except (InvalidOperation, ValueError):
        return Decimal("0")
    if rate < 0 or rate > 1:
        return Decimal("0")
    return rate


def ndfl_taxable_base_components(
    config: Mapping[str, Any],
    totals: Mapping[str, Any],
) -> tuple[Decimal, dict[str, Any]]:
    base_pay = decimal(totals.get("base_pay"))
    seniority = ndfl_seniority_allowance_total(totals)
    premium = decimal(totals.get("premium"))
    percent_pay = decimal(totals.get("percent_pay"))
    vacation_pay = decimal(totals.get("vacation_pay"))
    manual_penalties = decimal(totals.get("manual_deduction"))

    include_base_pay = ndfl_base_component_enabled(config, "base_pay", default=True)
    include_seniority = ndfl_base_component_enabled(config, "seniority_allowance", default=True)
    include_premium = ndfl_base_component_enabled(config, "premium", default=True)
    include_manual_bonuses = ndfl_base_component_enabled(config, "manual_bonuses", default=True)
    include_percent_pay = ndfl_base_component_enabled(config, "percent_pay", default=True)
    include_vacation_pay = ndfl_base_component_enabled(config, "vacation_pay", default=True)
    include_manual_penalties = ndfl_base_component_enabled(
        config,
        "manual_penalties",
        default=False,
    )

    taxable_base_pay = Decimal("0")
    if include_base_pay:
        taxable_base_pay = (
            base_pay if include_seniority else max(base_pay - seniority, Decimal("0"))
        )
    elif include_seniority:
        taxable_base_pay = seniority

    taxable_premium = premium if include_premium and include_manual_bonuses else Decimal("0")
    taxable_percent_pay = percent_pay if include_percent_pay else Decimal("0")
    taxable_vacation_pay = vacation_pay if include_vacation_pay else Decimal("0")
    taxable_manual_penalties = -manual_penalties if include_manual_penalties else Decimal("0")

    raw_base = (
        taxable_base_pay
        + taxable_premium
        + taxable_percent_pay
        + taxable_vacation_pay
        + taxable_manual_penalties
    )
    taxable_base = max(raw_base, Decimal("0"))
    return taxable_base, {
        "base_pay": ndfl_component_payload(base_pay, taxable_base_pay),
        "seniority_allowance": ndfl_component_payload(
            seniority,
            seniority if include_seniority else Decimal("0"),
            included_in="base_pay",
        ),
        "premium": ndfl_component_payload(premium, taxable_premium),
        "manual_bonuses": ndfl_component_payload(
            premium,
            taxable_premium,
            included_in="premium",
        ),
        "percent_pay": ndfl_component_payload(percent_pay, taxable_percent_pay),
        "vacation_pay": ndfl_component_payload(vacation_pay, taxable_vacation_pay),
        "manual_penalties": ndfl_component_payload(
            manual_penalties,
            taxable_manual_penalties,
            policy="excluded_by_default" if not include_manual_penalties else "subtracts_base",
        ),
    }


def ndfl_component_payload(
    amount: Decimal,
    taxable_amount: Decimal,
    *,
    included_in: str | None = None,
    policy: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "amount": money_string(amount),
        "taxable_amount": money_string(taxable_amount),
    }
    if included_in is not None:
        payload["included_in"] = included_in
    if policy is not None:
        payload["policy"] = policy
    return payload


def ndfl_base_config_payload(config: Mapping[str, Any]) -> dict[str, bool]:
    return {
        "base_pay": ndfl_base_component_enabled(config, "base_pay", default=True),
        "premium": ndfl_base_component_enabled(config, "premium", default=True),
        "percent_pay": ndfl_base_component_enabled(config, "percent_pay", default=True),
        "vacation_pay": ndfl_base_component_enabled(config, "vacation_pay", default=True),
        "manual_bonuses": ndfl_base_component_enabled(config, "manual_bonuses", default=True),
        "manual_penalties": ndfl_base_component_enabled(
            config,
            "manual_penalties",
            default=False,
        ),
        "seniority_allowance": ndfl_base_component_enabled(
            config,
            "seniority_allowance",
            default=True,
        ),
    }


def ndfl_seniority_allowance_total(totals: Mapping[str, Any]) -> Decimal:
    days = totals.get("days")
    if not isinstance(days, Iterable) or isinstance(days, str | bytes):
        return Decimal("0")
    return sum(
        (
            decimal(day.get("seniority_allowance_pay"))
            for day in days
            if isinstance(day, Mapping)
        ),
        Decimal("0"),
    )


def apply_ndfl_to_day_components(totals: Mapping[str, Any], ndfl: Mapping[str, Any]) -> None:
    days = totals.get("days")
    if not isinstance(days, list):
        return
    withheld = decimal(ndfl.get("withheld"))
    taxable_base = decimal(ndfl.get("taxable_base"))
    config = ndfl_config_from_components(ndfl.get("components"))
    bonus_by_date = ndfl_adjustment_amounts_by_date(totals, "bonuses")
    penalty_by_date = ndfl_adjustment_amounts_by_date(totals, "penalties")
    day_bases = [
        ndfl_day_taxable_base(config, day, bonus_by_date, penalty_by_date)
        if isinstance(day, dict)
        else Decimal("0")
        for day in days
    ]
    if taxable_base <= 0:
        day_bases = [Decimal("0") for _day in days]

    day_base_total = sum(day_bases, Decimal("0"))
    if day_bases and taxable_base > day_base_total:
        day_bases[0] += taxable_base - day_base_total
        day_base_total = taxable_base

    allocated = Decimal("0")
    last_index = len(days) - 1
    for index, day in enumerate(days):
        if not isinstance(day, dict):
            continue
        day_base = max(day_bases[index], Decimal("0"))
        if withheld <= 0 or taxable_base <= 0:
            day_withheld = Decimal("0")
        elif index == last_index:
            day_withheld = withheld - allocated
        elif day_base_total > 0:
            day_withheld = ndfl_money(withheld * day_base / day_base_total)
            allocated += day_withheld
        else:
            day_withheld = Decimal("0")
        day["ndfl_taxable_base"] = money(day_base)
        day["ndfl_withheld"] = money(day_withheld)


def ndfl_config_from_components(components: Any) -> Mapping[str, Any]:
    if not isinstance(components, Mapping):
        return {}
    base_config = components.get("base_config")
    return {"base": base_config} if isinstance(base_config, Mapping) else {}


def ndfl_day_taxable_base(
    config: Mapping[str, Any],
    day: Mapping[str, Any],
    bonus_by_date: Mapping[date, Decimal],
    penalty_by_date: Mapping[date, Decimal],
) -> Decimal:
    work_date = date_or_none(day.get("date"))
    base_pay = decimal(day.get("base_pay"))
    seniority = decimal(day.get("seniority_allowance_pay"))
    percent_pay = decimal(day.get("percent_pay"))
    vacation_pay = decimal(day.get("vacation_pay"))
    bonus = bonus_by_date.get(work_date, Decimal("0")) if work_date is not None else Decimal("0")
    penalty = (
        penalty_by_date.get(work_date, Decimal("0")) if work_date is not None else Decimal("0")
    )

    total = Decimal("0")
    if ndfl_base_component_enabled(config, "base_pay", default=True):
        total += base_pay
        if not ndfl_base_component_enabled(config, "seniority_allowance", default=True):
            total -= seniority
    elif ndfl_base_component_enabled(config, "seniority_allowance", default=True):
        total += seniority
    if ndfl_base_component_enabled(config, "percent_pay", default=True):
        total += percent_pay
    if ndfl_base_component_enabled(config, "vacation_pay", default=True):
        total += vacation_pay
    if (
        ndfl_base_component_enabled(config, "premium", default=True)
        and ndfl_base_component_enabled(config, "manual_bonuses", default=True)
    ):
        total += bonus
    if ndfl_base_component_enabled(config, "manual_penalties", default=False):
        total -= penalty
    return max(total, Decimal("0"))


def ndfl_adjustment_amounts_by_date(
    totals: Mapping[str, Any],
    key: str,
) -> dict[date, Decimal]:
    adjustments = totals.get("adjustments")
    if not isinstance(adjustments, Mapping):
        return {}
    items = adjustments.get(key)
    if not isinstance(items, list):
        return {}
    result: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
    for item in items:
        if not isinstance(item, Mapping):
            continue
        work_date = date_or_none(item.get("work_date"))
        if work_date is None:
            continue
        result[work_date] += decimal(item.get("amount"))
    return dict(result)


def ndfl_base_component_enabled(
    config: Mapping[str, Any],
    key: str,
    *,
    default: bool,
) -> bool:
    base_config = config.get("base")
    if not isinstance(base_config, Mapping):
        base_config = config.get("include")
    if not isinstance(base_config, Mapping):
        return default
    return bool(base_config.get(key, default))


def ndfl_money(value: Any) -> Decimal:
    return decimal(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def normalized_config_values(value: Any) -> set[str]:
    return {normalized_key(item) for item in iterable_config_values(value) if normalized_key(item)}


def normalized_category_config_values(value: Any) -> set[str]:
    result: set[str] = set()
    for item in iterable_config_values(value):
        result.update(normalized_category_keys(item))
    return {key for key in result if key}


def string_config_values(value: Any) -> set[str]:
    return {str(item) for item in iterable_config_values(value) if str(item)}


def iterable_config_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, Iterable):
        return list(value)
    return [value]


def line_deposit_override(
    overrides: Mapping[tuple[uuid.UUID, str], Mapping[str, Any]],
    employee_id: uuid.UUID,
    role: str,
) -> Mapping[str, Any]:
    return overrides.get((employee_id, role), {})


def optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def apply_adjustments_to_line_totals(
    line_totals: dict[tuple[uuid.UUID, str], dict[str, Any]],
    settings: Mapping[str, Any],
    period: PayrollPeriod,
) -> None:
    adjustments_by_day = settings.get(PAYROLL_ADJUSTMENTS_CONFIG_KEY)
    if not isinstance(adjustments_by_day, Mapping):
        return

    roles_by_employee: dict[uuid.UUID, list[str]] = defaultdict(list)
    for employee_id, role in line_totals:
        roles_by_employee[employee_id].append(role)

    for employee_id, roles in roles_by_employee.items():
        adjustments = adjustments_for_employee_period(
            adjustments_by_day,
            employee_id,
            period.start_date,
            period.end_date,
        )
        if not adjustments:
            continue

        # Manual adjustments are employee-level. If a person has several payroll
        # lines in the week, attach them to the primary assignment role.
        target_role = adjustment_target_role(settings, employee_id, roles, period)
        for role in roles:
            line_totals[(employee_id, role)]["adjustments"]["primary_role_chosen"] = target_role

        totals = line_totals[(employee_id, target_role)]
        for adjustment in adjustments:
            amount = decimal(getattr(adjustment, "amount", 0))
            item = adjustment_component(adjustment)
            if getattr(adjustment, "type", "") == "bonus":
                totals["premium"] += amount
                totals["adjustments"]["bonuses"].append(item)
            elif getattr(adjustment, "type", "") == "penalty":
                totals["manual_deduction"] += amount
                totals["adjustments"]["penalties"].append(item)


def adjustments_for_employee_period(
    adjustments_by_day: Mapping[Any, Any],
    employee_id: uuid.UUID,
    period_start: date,
    period_end: date,
) -> list[PayrollAdjustment]:
    adjustments: list[PayrollAdjustment] = []
    for value in adjustments_by_day.values():
        if isinstance(value, Iterable) and not isinstance(value, str | bytes):
            candidates = value
        else:
            candidates = [value]
        for adjustment in candidates:
            adjustment_employee_id = getattr(adjustment, "employee_id", None)
            work_date = date_or_none(getattr(adjustment, "work_date", None))
            if (
                adjustment_employee_id == employee_id
                and work_date is not None
                and period_start <= work_date <= period_end
            ):
                adjustments.append(adjustment)
    return sorted(
        adjustments,
        key=lambda adjustment: (
            date_or_none(getattr(adjustment, "work_date", None)) or date.min,
            str(getattr(adjustment, "id", "")),
        ),
    )


def adjustment_target_role(
    settings: Mapping[str, Any],
    employee_id: uuid.UUID,
    roles: list[str],
    period: PayrollPeriod,
) -> str:
    sorted_roles = sorted(roles)
    current = period.start_date
    while current <= period.end_date:
        assignments = assignments_for_employee_date(settings, employee_id, current)
        primary = next(
            (assignment for assignment in assignments if getattr(assignment, "is_primary", False)),
            None,
        )
        if primary is not None:
            matched_role = matching_line_role(getattr(primary, "payroll_role", None), sorted_roles)
            if matched_role is not None:
                return matched_role
        current = date.fromordinal(current.toordinal() + 1)
    return sorted_roles[0]


def matching_line_role(assignment_role: Any, line_roles: Iterable[str]) -> str | None:
    lookup_keys = {normalized_key(role) for role in role_lookup_keys(assignment_role)}
    for line_role in line_roles:
        if normalized_key(line_role) in lookup_keys:
            return line_role
    return None


def adjustment_component(adjustment: PayrollAdjustment) -> dict[str, Any]:
    return {
        "id": str(getattr(adjustment, "id", "")),
        "work_date": date_string(getattr(adjustment, "work_date", None)),
        "category": adjustment_category_name(adjustment),
        "amount": money_string(getattr(adjustment, "amount", 0)),
        "comment": getattr(adjustment, "comment", None),
    }


def adjustment_category_name(adjustment: PayrollAdjustment) -> str:
    category = getattr(adjustment, "category", None)
    if category is not None:
        display_name = getattr(category, "display_name", None)
        if display_name:
            return str(display_name)
    return str(getattr(adjustment, "custom_label", "") or "")


def validate_calculation_inputs(
    entries: Iterable[AttendanceEntry],
    employees: Mapping[uuid.UUID, Employee],
    settings: Mapping[str, Any],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    vacation_days = vacation_days_from_settings(settings)
    missing_settings = sorted(
        PAYROLL_SETTING_KEYS - OPTIONAL_PAYROLL_SETTING_KEYS - settings.keys()
    )
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
        if (entry.employee_id, entry.work_date) in vacation_days:
            continue
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


def vacation_role_for_employee_day(
    settings: Mapping[str, Any],
    employee: Employee,
    work_date: date,
) -> str:
    assignments = assignments_for_employee_date(settings, employee.id, work_date)
    primary = next(
        (
            assignment
            for assignment in assignments
            if getattr(assignment, "is_primary", False)
            and getattr(assignment, "payroll_role", None)
        ),
        None,
    )
    if primary is not None:
        return str(primary.payroll_role)
    first = next(
        (assignment for assignment in assignments if getattr(assignment, "payroll_role", None)),
        None,
    )
    if first is not None:
        return str(first.payroll_role)
    fallback_by_position = {"Кассир": "administrator", "Повар": "prep"}
    return fallback_by_position.get(str(employee.position or ""), "vacation")


def vacation_days_from_settings(settings: Mapping[str, Any]) -> set[tuple[uuid.UUID, date]]:
    raw = settings.get(VACATION_DAYS_CONFIG_KEY)
    if not isinstance(raw, Mapping):
        return set()
    result: set[tuple[uuid.UUID, date]] = set()
    for key in raw:
        if isinstance(key, tuple) and len(key) == 2:
            employee_value, date_value = key
        elif isinstance(key, str) and ":" in key:
            employee_value, date_value = key.split(":", 1)
        else:
            continue
        try:
            employee_id = (
                employee_value
                if isinstance(employee_value, uuid.UUID)
                else uuid.UUID(str(employee_value))
            )
            work_date = (
                date_value if isinstance(date_value, date) else date.fromisoformat(str(date_value))
            )
        except (TypeError, ValueError):
            continue
        result.add((employee_id, work_date))
    return result


def vacation_daily_amount_from_settings(settings: Mapping[str, Any]) -> Decimal:
    return decimal(
        settings.get(VACATION_DAILY_AMOUNT_CONFIG_KEY, vacation_service.DEFAULT_DAILY_AMOUNT)
    )


def category_for_payroll_entry(
    settings: Mapping[str, Any],
    employee: Employee,
    work_date: date,
    role: str | None,
    station: str | None,
) -> str:
    assignments_value = employee_date_config_value(
        settings,
        EMPLOYEE_ASSIGNMENTS_CONFIG_KEY,
        employee.id,
        work_date,
    )
    assignments_loaded_for_day = assignments_value is not _MISSING_CONFIG_VALUE
    # Live employee role assignments win over the ledger snapshot so that
    # back-dated category or role changes in the staff page propagate into
    # the payroll calculation on the next Пересчитать.
    assignments = assignments_from_config_value(assignments_value)
    if assignments:
        assignment_role = assignment_role_for_payroll_context(role, station)
        if assignment_role:
            for assignment in assignments:
                category = getattr(assignment, "category", None)
                if getattr(assignment, "payroll_role", None) == assignment_role and category:
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

    ledger_entry = ledger_entry_for_employee_date(settings, employee.id, work_date)
    if ledger_entry is not None:
        ledger_category = clean_string(getattr(ledger_entry, "category", None))
        if ledger_category:
            return ledger_category

    if assignments_loaded_for_day:
        return ""

    # No active assignment or ledger category for work_date — fall back to the
    # current value on the employee card.
    if employee.category:
        return str(employee.category)

    return ""


def is_substitute_payroll_entry(
    settings: Mapping[str, Any],
    employee: Employee,
    work_date: date,
    role: str | None,
    station: str | None,
) -> bool:
    assignments = assignments_for_employee_date(settings, employee.id, work_date)
    if not assignments:
        return False
    assignment_role = assignment_role_for_payroll_context(role, station) or role
    if not assignment_role:
        return False
    return any(
        getattr(assignment, "payroll_role", None) == assignment_role
        and bool(getattr(assignment, "is_substitute", False))
        for assignment in assignments
    )


def position_for_payroll_entry(
    settings: Mapping[str, Any],
    employee: Employee,
    work_date: date | None,
) -> str | None:
    if work_date is None:
        return employee.position
    positions_by_day = settings.get(EMPLOYEE_POSITIONS_CONFIG_KEY)
    if isinstance(positions_by_day, Mapping):
        value = positions_by_day.get((employee.id, work_date))
        if value is None:
            value = positions_by_day.get((str(employee.id), work_date.isoformat()))
        if value is not None:
            return str(value)
    return employee.position


def allowance_flags_for_payroll_entry(
    settings: Mapping[str, Any],
    employee: Employee,
    work_date: date | None,
) -> dict[str, bool]:
    fallback = {
        "is_senior": bool(employee.is_senior),
        "is_deputy_senior": bool(employee.is_deputy_senior),
    }
    if work_date is None:
        return fallback
    value = employee_date_config_value(
        settings,
        EMPLOYEE_ALLOWANCES_CONFIG_KEY,
        employee.id,
        work_date,
    )
    if value is _MISSING_CONFIG_VALUE:
        return fallback
    if not isinstance(value, Mapping):
        return {"is_senior": False, "is_deputy_senior": False}
    return {
        "is_senior": bool(value.get("is_senior", False)),
        "is_deputy_senior": bool(value.get("is_deputy_senior", False)),
    }


def ledger_entry_for_employee_date(
    settings: Mapping[str, Any],
    employee_id: uuid.UUID,
    work_date: date,
) -> Any | None:
    value = employee_date_config_value(settings, SHIFT_LEDGER_CONFIG_KEY, employee_id, work_date)
    return None if value is _MISSING_CONFIG_VALUE else value


def assignments_for_employee_date(
    settings: Mapping[str, Any],
    employee_id: uuid.UUID,
    work_date: date,
) -> list[Any]:
    value = employee_date_config_value(
        settings,
        EMPLOYEE_ASSIGNMENTS_CONFIG_KEY,
        employee_id,
        work_date,
    )
    return assignments_from_config_value(value)


def assignments_from_config_value(value: Any) -> list[Any]:
    return list(value) if isinstance(value, Iterable) and not isinstance(value, str | bytes) else []


def employee_date_config_value(
    settings: Mapping[str, Any],
    config_key: str,
    employee_id: uuid.UUID,
    work_date: date,
) -> Any:
    values_by_day = settings.get(config_key)
    if not isinstance(values_by_day, Mapping):
        return _MISSING_CONFIG_VALUE
    tuple_key = (employee_id, work_date)
    if tuple_key in values_by_day:
        return values_by_day[tuple_key]
    string_key = (str(employee_id), work_date.isoformat())
    if string_key in values_by_day:
        return values_by_day[string_key]
    return _MISSING_CONFIG_VALUE


def role_category_rate_exists(
    settings: Mapping[str, Any],
    role: str,
    category: str,
    work_date: date | None = None,
    station: str | None = None,
) -> bool:
    return role_category_rate_details(settings, role, category, work_date, station) is not None


def role_category_rate(
    settings: Mapping[str, Any],
    role: str,
    category: str,
    work_date: date | None = None,
    station: str | None = None,
) -> Decimal | None:
    details = role_category_rate_details(settings, role, category, work_date, station)
    if details is None:
        return None
    return decimal_or_none(details.get("amount"))


def role_category_rate_details(
    settings: Mapping[str, Any],
    role: str,
    category: str,
    work_date: date | None = None,
    station: str | None = None,
) -> dict[str, Any] | None:
    if category_rule_key(category) == "6":
        return None

    versioned_rate = role_category_rate_details_from_versions(
        settings,
        role,
        category,
        work_date,
        station,
    )
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
    amount = decimal_or_none(value)
    if amount is None:
        return None
    return {
        "source": "legacy_setting",
        "position_group": role,
        "category": category,
        "station": station,
        "rate_type": "daily",
        "amount": amount,
        "effective_from": None,
        "effective_to": None,
    }


def role_category_rate_from_versions(
    settings: Mapping[str, Any],
    role: str,
    category: str,
    work_date: date | None,
    station: str | None,
) -> Decimal | None:
    details = role_category_rate_details_from_versions(settings, role, category, work_date, station)
    if details is None:
        return None
    return decimal_or_none(details.get("amount"))


def role_category_rate_details_from_versions(
    settings: Mapping[str, Any],
    role: str,
    category: str,
    work_date: date | None,
    station: str | None,
) -> dict[str, Any] | None:
    rates = settings.get(PAYROLL_RATE_CONFIG_KEY)
    if not isinstance(rates, list):
        return None

    candidates: list[tuple[int, date, Decimal, Mapping[str, Any], date | None, date | None]] = []
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
        # Если у ставки задана станция, а в query станция тоже задана и не совпадает — отсекаем.
        # Если у ставки задана станция, а query пустой — НЕ отсекаем, понижаем приоритет через
        # station_score ниже (fallback на ставку «без точной привязки к станции»).
        if item_station and normalized_station and item_station != normalized_station:
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
        candidates.append(
            (station_score, effective_from or date.min, amount, item, effective_from, effective_to)
        )

    if not candidates:
        return None
    candidates.sort(key=lambda candidate: (candidate[0], candidate[1]), reverse=True)
    _station_score, _effective_sort, amount, item, effective_from, effective_to = candidates[0]
    return {
        "source": str(item.get("source") or "payroll_rate"),
        "id": optional_string(item.get("id") or item.get("payroll_rate_id")),
        "position_group": str(item.get("position_group") or role),
        "category": str(item.get("category") or category),
        "station": optional_string(item.get("station")),
        "rate_type": str(item.get("rate_type") or "daily"),
        "amount": amount,
        "effective_from": effective_from,
        "effective_to": effective_to,
    }


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
    return rate * shift_pay_ratio(minutes)


def seniority_allowance_for_payroll_entry(
    settings: Mapping[str, Any],
    employee: Employee,
    minutes: int,
    work_date: date | None,
) -> Decimal:
    amount, _metadata = seniority_allowance_details_for_payroll_entry(
        settings,
        employee,
        minutes,
        work_date,
    )
    return amount


def seniority_allowance_details_for_payroll_entry(
    settings: Mapping[str, Any],
    employee: Employee,
    minutes: int,
    work_date: date | None,
) -> tuple[Decimal, dict[str, Any]]:
    allowance_flags = allowance_flags_for_payroll_entry(settings, employee, work_date)
    position = position_for_payroll_entry(settings, employee, work_date)
    if position == CASHIER_POSITION:
        cashier_assignments = settings.get(CASHIER_ALLOWANCE_ASSIGNMENTS_CONFIG_KEY)
        if isinstance(cashier_assignments, Mapping):
            assignment = cashier_allowance_assignment_for_date(
                cashier_assignments,
                work_date,
            )
            return cashier_seniority_allowance_for_assignment(
                settings,
                employee,
                minutes,
                work_date,
                assignment,
                allowance_flags,
            )

    allowance_role: str | None = None
    if allowance_flags["is_senior"]:
        allowance_role = "senior"
    elif allowance_flags["is_deputy_senior"]:
        allowance_role = "deputy_senior"
    if allowance_role is None:
        return Decimal("0"), {}

    amount = allowance_amount_from_settings(
        settings,
        position=position,
        role=allowance_role,
        on_date=work_date,
    )
    return amount * shift_pay_ratio(minutes), {
        "seniority_allowance_role": allowance_role,
    }


def cashier_seniority_allowance_for_assignment(
    settings: Mapping[str, Any],
    employee: Employee,
    minutes: int,
    work_date: date | None,
    assignment: Any,
    allowance_flags: Mapping[str, bool],
) -> tuple[Decimal, dict[str, Any]]:
    reason = assignment_reason(assignment) or "no_candidate"
    recipient_employee_id = assignment_recipient_employee_id(assignment)
    recipient_role = assignment_recipient_role(assignment)
    is_recipient = recipient_employee_id is not None and recipient_employee_id == employee.id

    metadata: dict[str, Any] = {
        "seniority_allowance_reason": reason,
    }
    if is_recipient and recipient_role in {"senior", "deputy_senior"}:
        amount = allowance_amount_from_settings(
            settings,
            position=CASHIER_POSITION,
            role=recipient_role,
            on_date=work_date,
        )
        metadata["seniority_allowance_role"] = recipient_role
        return amount * shift_pay_ratio(minutes), metadata

    if allowance_flags.get("is_senior") or allowance_flags.get("is_deputy_senior"):
        metadata["seniority_allowance_skipped_reason"] = cashier_skipped_reason(
            reason,
            recipient_employee_id,
        )
    return Decimal("0"), metadata


def cashier_allowance_assignment_for_date(
    assignments: Mapping[Any, Any],
    work_date: date | None,
) -> Any:
    if work_date is None:
        return None
    value = assignments.get(work_date)
    if value is None:
        value = assignments.get(work_date.isoformat())
    return value


def assignment_recipient_employee_id(assignment: Any) -> uuid.UUID | None:
    if assignment is None:
        return None
    value = (
        assignment.get("recipient_employee_id")
        if isinstance(assignment, Mapping)
        else getattr(assignment, "recipient_employee_id", None)
    )
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str) and value:
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


def assignment_recipient_role(assignment: Any) -> str | None:
    if assignment is None:
        return None
    value = (
        assignment.get("recipient_role")
        if isinstance(assignment, Mapping)
        else getattr(assignment, "recipient_role", None)
    )
    return str(value) if value is not None else None


def assignment_reason(assignment: Any) -> str | None:
    if assignment is None:
        return None
    value = (
        assignment.get("reason")
        if isinstance(assignment, Mapping)
        else getattr(assignment, "reason", None)
    )
    return str(value) if value is not None else None


def cashier_skipped_reason(
    assignment_reason_value: str,
    recipient_employee_id: uuid.UUID | None,
) -> str:
    if assignment_reason_value == "manual_override":
        return "manual_override"
    if recipient_employee_id is not None:
        return (
            "plan_priority_to_other"
            if assignment_reason_value == "plan_priority"
            else assignment_reason_value
        )
    return assignment_reason_value


def weekday_premium_for_employee_day(
    settings: Mapping[str, Any],
    work_date: date,
    minutes_worked: int,
) -> Decimal:
    if work_date.weekday() not in WEEKDAY_PREMIUM_WEEKDAYS:
        return Decimal("0")

    config = settings.get("payroll.weekday_premium", {})
    threshold_minutes = weekday_premium_threshold_minutes(config)
    if Decimal(minutes_worked) < threshold_minutes:
        return Decimal("0")
    return weekday_premium_amount(config, work_date)


def normalize_weekday_premium_setting(value: Any, widget_options: Any | None) -> Any:
    if not isinstance(value, Mapping):
        return value
    options = widget_options if isinstance(widget_options, Mapping) else {}
    if "threshold_hours" in value or "threshold_hours" not in options:
        return value
    return {**value, "threshold_hours": options["threshold_hours"]}


def weekday_premium_amount(config: Any, work_date: date) -> Decimal:
    if not isinstance(config, Mapping):
        return DEFAULT_WEEKDAY_PREMIUM_AMOUNT
    if "amount" in config:
        return decimal(config["amount"])
    weekday_key = WEEKDAY_KEYS[work_date.weekday()]
    if weekday_key in config:
        return decimal(config[weekday_key])
    return DEFAULT_WEEKDAY_PREMIUM_AMOUNT


def weekday_premium_threshold_minutes(config: Any) -> Decimal:
    if isinstance(config, Mapping) and "threshold_hours" in config:
        threshold_hours = decimal(config["threshold_hours"])
    else:
        threshold_hours = DEFAULT_WEEKDAY_PREMIUM_THRESHOLD_HOURS
    return threshold_hours * Decimal(60)


def shift_pay_ratio(minutes: int) -> Decimal:
    return Decimal(minutes) / FULL_SHIFT_MINUTES


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
    for key in (DAILY_REVENUE_CONFIG_KEY, "payroll.mock_daily_revenue"):
        revenue_by_day = settings.get(key, {})
        if isinstance(revenue_by_day, Mapping) and work_date.isoformat() in revenue_by_day:
            return decimal(revenue_by_day.get(work_date.isoformat(), 0))
    return Decimal("0")


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
    return fund_accrual_for_day(settings, employee, period_end, base_pay)


def tenure_months_on(employee: Employee, on_date: date) -> int:
    base = getattr(employee, "tenure_started_at", None) or employee.hire_date
    if base is None:
        return 0
    months = (on_date.year - base.year) * 12 + (on_date.month - base.month)
    if on_date.day < base.day:
        months -= 1
    return max(months, 0)


def fund_accrual_for_day(
    settings: Mapping[str, Any],
    employee: Employee,
    work_date: date,
    base_pay_with_premium: Decimal,
) -> Decimal:
    if employee.position not in PAYROLL_TARGET_POSITIONS:
        return Decimal("0")
    base_pay_with_premium = decimal(base_pay_with_premium)
    if base_pay_with_premium <= 0:
        return Decimal("0")
    if is_fund_currently_excluded(employee, work_date):
        return Decimal("0")
    months = tenure_months_on(employee, work_date)
    rate = _fund_rate_for_months(settings, months)
    return (base_pay_with_premium * rate).to_integral_value(rounding=ROUND_FLOOR)


def _fund_rate_for_months(settings: Mapping[str, Any], months: int) -> Decimal:
    rate = Decimal("0")
    for tier in settings.get("payroll.fund_rates_by_tenure", []):
        if not isinstance(tier, Mapping):
            continue
        if "min_months" in tier:
            threshold = int(decimal(tier["min_months"]))
        elif "min_years" in tier:
            threshold = int(decimal(tier["min_years"]) * Decimal("12"))
        else:
            continue
        if months >= threshold:
            rate = decimal(tier.get("rate", 0))
    return rate


def deposit_withholding(
    settings: Mapping[str, Any],
    employee: Employee,
    payable_before_deduction: Decimal,
    today: date | None = None,
    current_balance: Decimal = Decimal("0"),
) -> Decimal:
    if not settings.get("payroll.deposit_auto_withholding_enabled"):
        return Decimal("0")
    payable_before_deduction = decimal(payable_before_deduction)
    if payable_before_deduction <= 0:
        return Decimal("0")

    today = today or date.today()
    if is_deposit_currently_excluded(employee, today):
        return Decimal("0")

    target = employee_deposit_target(settings, employee)
    if target is None or target <= 0:
        return Decimal("0")

    withholding = employee_deposit_withholding(settings, employee)
    if withholding is None or withholding <= 0:
        return Decimal("0")

    remaining_to_target = max(target - decimal(current_balance), Decimal("0"))
    effective_withholding = min(withholding, remaining_to_target)
    return min(effective_withholding, payable_before_deduction)


def is_deposit_currently_excluded(employee: Employee, today: date) -> bool:
    if not getattr(employee, "deposit_excluded", False):
        return False
    excluded_until = getattr(employee, "deposit_excluded_until", None)
    if excluded_until is None:
        return True
    return today <= excluded_until


def is_fund_currently_excluded(employee: Employee, today: date) -> bool:
    if not getattr(employee, "fund_excluded", False):
        return False
    excluded_until = getattr(employee, "fund_excluded_until", None)
    if excluded_until is None:
        return True
    return today <= excluded_until


def employee_deposit_target(
    settings: Mapping[str, Any],
    employee: Employee,
) -> Decimal | None:
    override = getattr(employee, "deposit_target_override", None)
    if override is not None:
        return decimal(override)
    return category_deposit_target(settings, employee.category)


def employee_deposit_withholding(
    settings: Mapping[str, Any],
    employee: Employee,
) -> Decimal | None:
    override = getattr(employee, "deposit_withholding_override", None)
    if override is not None:
        return decimal(override)
    return category_deposit_withholding(settings, employee.category)


def category_deposit_target(settings: Mapping[str, Any], category: Any) -> Decimal | None:
    return category_deposit_value(settings, category, "deposit_target")


def category_deposit_withholding(settings: Mapping[str, Any], category: Any) -> Decimal | None:
    return category_deposit_value(settings, category, "deposit_withholding")


def category_deposit_value(
    settings: Mapping[str, Any],
    category: Any,
    key: str,
) -> Decimal | None:
    rules_by_category = settings.get("payroll.category_rules") or {}
    if not isinstance(rules_by_category, Mapping):
        return None
    rules = rules_by_category.get(category_rule_key(category))
    if not isinstance(rules, Mapping) or key not in rules:
        return None
    return decimal_or_none(rules.get(key))


def summarize_lines(lines: Iterable[PayrollLine]) -> dict[str, Any]:
    lines = list(lines)
    return {
        "line_count": len(lines),
        "base_pay": money(sum((decimal(line.base_pay) for line in lines), Decimal("0"))),
        "premium": money(sum((decimal(line.premium) for line in lines), Decimal("0"))),
        "percent_pay": money(sum((decimal(line.percent_pay) for line in lines), Decimal("0"))),
        "vacation_pay": money(sum((decimal(line.vacation_pay) for line in lines), Decimal("0"))),
        "ndfl_withheld": money(
            sum((decimal(getattr(line, "ndfl_withheld", 0)) for line in lines), Decimal("0"))
        ),
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


def money_string(value: Any) -> str:
    return f"{decimal(value).quantize(MONEY)}"


def payroll_rate_snapshot_payload(
    rates: Iterable[Mapping[str, Any]],
    *,
    locked: bool,
) -> dict[str, Any]:
    return {
        "locked": locked,
        "rates": [serialize_payroll_rate_snapshot(rate) for rate in rates],
    }


def payroll_rate_component(rate: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if rate is None:
        return None
    return serialize_payroll_rate_snapshot(rate)


def serialize_payroll_rate_snapshot(rate: Mapping[str, Any]) -> dict[str, Any]:
    amount = decimal_or_none(rate.get("amount"))
    return {
        "source": str(rate.get("source") or "payroll_rate"),
        "id": optional_string(rate.get("id") or rate.get("payroll_rate_id")),
        "position_group": optional_string(rate.get("position_group")),
        "category": optional_string(rate.get("category")),
        "station": optional_string(rate.get("station")),
        "rate_type": str(rate.get("rate_type") or "daily"),
        "amount": money_string(amount) if amount is not None else None,
        "effective_from": optional_date_string(rate.get("effective_from")),
        "effective_to": optional_date_string(rate.get("effective_to")),
    }


def date_string(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")


def optional_date_string(value: Any) -> str | None:
    parsed = date_or_none(value)
    return parsed.isoformat() if parsed is not None else None


def optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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

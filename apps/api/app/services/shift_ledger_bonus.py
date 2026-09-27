"""Live, read-only revenue bonus preview from the shift ledger (no payroll run needed)."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EmployeeRoleAssignment
from app.services import vacation_service
from app.services.attendance_loader import MOSCOW_TZ
from app.services.iiko_revenue import fetch_daily_revenue
from app.services.payroll_calculator import (
    EMPLOYEE_ASSIGNMENTS_CONFIG_KEY,
    SHIFT_LEDGER_CONFIG_KEY,
    category_for_payroll_entry,
    merge_attendance_minutes,
)
from app.services.payroll_percent import (
    PercentShift,
    category_coefficient,
    compute_daily_percent_pool,
    distribute_day_percent,
    load_category_coefficient_versions,
    load_revenue_tier_versions,
    revenue_tier_rate,
    shift_weight,
)
from app.services.shift_ledger import iter_dates, ledger_week_bounds, load_ledger_matrix_rows
from app.services.staff_taxonomy import PAYROLL_ROLE_LABELS


async def calculate_ledger_bonuses(
    session: AsyncSession,
    selected_date: date,
    *,
    now: datetime | None = None,
) -> dict:
    """Use the payroll engine's tiers, date-effective categories, hours and rounding.

    All participants of a day enter the denominator, including staff not expanded in
    the UI. Ledger intervals are used directly: AttendanceEntry is only populated by
    payroll runs and is therefore not a source for a live preview. Nothing is accrued
    or written to payroll/cache by this read endpoint.
    """
    now = now or datetime.now(UTC)
    start_date, end_date = ledger_week_bounds(selected_date)
    rows = await load_ledger_matrix_rows(session, start_date, end_date)
    # One live OLAP request for all seven days. Never replace a fetch failure with 0
    # or silently use the payroll cache, which may be days old.
    try:
        revenues = await fetch_daily_revenue(session, start_date, end_date)
    except SystemExit as exc:
        # The legacy iiko client exits on missing configuration; a read-only
        # preview must return an unavailable state instead of stopping the API.
        raise RuntimeError("iiko revenue client is not configured") from exc
    tiers = await load_revenue_tier_versions(session, start_date, end_date)
    coefficients = await load_category_coefficient_versions(session, start_date, end_date)
    vacation_days = await vacation_service.vacation_days_for_payroll_period(
        session, period_start=start_date, period_end=end_date
    )
    employee_ids = {entry.employee_id for entry, _employee in rows}
    assignments = (
        list(
            (
                await session.scalars(
                    select(EmployeeRoleAssignment)
                    .where(
                        EmployeeRoleAssignment.employee_id.in_(employee_ids),
                        EmployeeRoleAssignment.effective_from <= end_date,
                        or_(
                            EmployeeRoleAssignment.effective_to.is_(None),
                            EmployeeRoleAssignment.effective_to > start_date,
                        ),
                    )
                    .order_by(
                        EmployeeRoleAssignment.is_primary.desc(),
                        EmployeeRoleAssignment.payroll_role,
                    )
                )
            ).all()
        )
        if employee_ids
        else []
    )
    assignments_by_employee: dict[uuid.UUID, list[EmployeeRoleAssignment]] = defaultdict(list)
    for assignment in assignments:
        assignments_by_employee[assignment.employee_id].append(assignment)
    rows_by_day: dict[date, list] = defaultdict(list)
    for entry, employee in rows:
        rows_by_day[entry.work_date].append((entry, employee))

    days = []
    for work_date in iter_dates(start_date, end_date):
        revenue = revenues.get(work_date, Decimal("0"))
        grouped_intervals: dict[tuple[uuid.UUID, str, str], list] = defaultdict(list)
        participants = {entry.employee_id for entry, _employee in rows_by_day[work_date]}
        needs_review = False
        has_open_shifts = False
        for entry, employee in rows_by_day[work_date]:
            if (entry.employee_id, work_date) in vacation_days:
                continue
            if not entry.is_resolved or entry.payroll_role not in PAYROLL_ROLE_LABELS:
                needs_review = True
                continue
            active_assignments = [
                assignment
                for assignment in assignments_by_employee[employee.id]
                if assignment.effective_from <= work_date
                and (assignment.effective_to is None or assignment.effective_to > work_date)
            ]
            settings = {
                EMPLOYEE_ASSIGNMENTS_CONFIG_KEY: {(employee.id, work_date): active_assignments},
                SHIFT_LEDGER_CONFIG_KEY: {(employee.id, work_date): entry},
            }
            category = category_for_payroll_entry(
                settings, employee, work_date, entry.payroll_role, None
            )
            if not category:
                needs_review = True
                continue
            end = entry.closed_at
            if end is None:
                has_open_shifts = True
                # A forgotten open shift is not a confirmed twelve-hour shift.
                if work_date < now.astimezone(
                    MOSCOW_TZ
                ).date() and now - entry.opened_at > timedelta(hours=12):
                    needs_review = True
                    continue
                end = now
            if end < entry.opened_at or entry.opened_at > now or end > now:
                needs_review = True
                continue
            grouped_intervals[(employee.id, entry.payroll_role, category)].append(
                (entry.opened_at, end, 0)
            )

        shifts = [
            PercentShift(
                employee_id=key,
                category=key[2],
                hours=Decimal(merge_attendance_minutes(intervals)) / Decimal(60),
                coefficient=category_coefficient(key[2], work_date, coefficients),
            )
            for key, intervals in grouped_intervals.items()
        ]
        distribution = distribute_day_percent(revenue, work_date, tiers, shifts)
        employees = {
            employee_id: {"employee_id": employee_id, "percent": Decimal("0"), "shifts": []}
            for employee_id in participants
        }
        for shift in shifts:
            employee_id, role, category = shift.employee_id
            percent = distribution[shift.employee_id]
            employees[employee_id]["percent"] += percent
            employees[employee_id]["shifts"].append(
                {
                    "role": role,
                    "category": category,
                    "hours": shift.hours,
                    "coefficient": shift.coefficient,
                    "weight": shift_weight(shift),
                    "percent": percent,
                }
            )
        days.append(
            {
                "date": work_date,
                "daily_revenue": revenue.quantize(Decimal("0.01")),
                "percent_pool": compute_daily_percent_pool(revenue, work_date, tiers).quantize(
                    Decimal("0.01")
                ),
                "rate_percent": (revenue_tier_rate(revenue, work_date, tiers) or Decimal("0"))
                * 100,
                "status": "needs_review" if needs_review else "ready",
                "has_open_shifts": has_open_shifts,
                # A missing participant weight changes everyone's share. Do not show a
                # confident amount for other employees until the day's inputs are fixed.
                "employees": [] if needs_review else list(employees.values()),
            }
        )
    return {"calculated_at": now, "days": days}

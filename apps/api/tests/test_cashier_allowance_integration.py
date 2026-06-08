from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from app.models import AttendanceEntry, Employee, PayrollPeriod, ScheduledShift
from app.services.payroll_calculator import calculate_payroll_lines_from_inputs
from app.services.seniority_allowance_resolver import (
    CASHIER_ALLOWANCE_ASSIGNMENTS_CONFIG_KEY,
    AllowanceCandidate,
    resolve_cashier_allowance_recipient,
)
from app.services.seniority_allowance_service import SENIORITY_ALLOWANCE_MAP_CONFIG_KEY
from app.services.shift_cost_estimate_service import compute_shift_cost


def payroll_settings(work_date: date, assignment: Any | None = None) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "payroll.role_category_rates": {
            "Пиццерист": {"category_2": 2200},
            "Администратор": {"category_3": 2000},
        },
        "payroll.category_rules": {
            "2": {"coeff": 7.5, "deposit_target": 15000, "deposit_withholding": 1000},
            "3": {"coeff": 5, "deposit_target": 10000, "deposit_withholding": 1000},
        },
        "payroll.weekday_premium": {"amount": 200, "threshold_hours": 8},
        "payroll.fund_rates_by_tenure": [],
        "payroll.mock_daily_revenue": {},
        "payroll.deposit_auto_withholding_enabled": False,
        "payroll.deposit_fund_payment_date": "01-15",
        SENIORITY_ALLOWANCE_MAP_CONFIG_KEY: {
            work_date: {
                ("Повар", "senior"): Decimal("600"),
                ("Повар", "deputy_senior"): Decimal("400"),
                ("Кассир", "senior"): Decimal("500"),
                ("Кассир", "deputy_senior"): Decimal("300"),
            }
        },
    }
    if assignment is not None:
        settings[CASHIER_ALLOWANCE_ASSIGNMENTS_CONFIG_KEY] = {work_date: assignment}
    return settings


def make_period(work_date: date) -> PayrollPeriod:
    return PayrollPeriod(
        id=uuid.uuid4(),
        period_type="week",
        start_date=work_date,
        end_date=work_date,
        payroll_date=work_date,
        status="open",
    )


def employee(
    *,
    position: str,
    category: str,
    senior: bool = False,
    deputy: bool = False,
    full_name: str = "Иванов Иван",
) -> Employee:
    return Employee(
        id=uuid.uuid4(),
        full_name=full_name,
        iiko_id=f"iiko-{uuid.uuid4()}",
        position=position,
        category=category,
        status="active",
        hire_date=date(2025, 1, 1),
        tenure_started_at=date(2025, 1, 1),
        is_senior=senior,
        is_deputy_senior=deputy,
    )


def entry(
    period: PayrollPeriod,
    item: Employee,
    work_date: date,
    *,
    minutes: int = 720,
) -> AttendanceEntry:
    role = "Администратор" if item.position == "Кассир" else "Пиццерист"
    return AttendanceEntry(
        id=uuid.uuid4(),
        employee_id=item.id,
        period_id=period.id,
        work_date=work_date,
        started_at=datetime.combine(work_date, datetime.min.time(), tzinfo=UTC),
        ended_at=datetime.combine(work_date, datetime.min.time(), tzinfo=UTC),
        minutes_worked=minutes,
        role=role,
        station=None,
        source="manual",
        quality_status="ok",
    )


def candidate(item: Employee, *, planned: bool = True, actual: bool = True) -> AllowanceCandidate:
    return AllowanceCandidate(
        employee_id=item.id,
        full_name=item.full_name,
        is_senior=bool(item.is_senior),
        is_deputy_senior=bool(item.is_deputy_senior),
        is_planned=planned,
        is_actual=actual,
        minutes_worked=720,
    )


def test_payroll_cashier_only_recipient_gets_allowance() -> None:
    work_date = date(2026, 6, 1)
    period = make_period(work_date)
    senior = employee(position="Кассир", category="category_3", senior=True)
    deputy = employee(position="Кассир", category="category_3", deputy=True)
    assignment = resolve_cashier_allowance_recipient(
        candidates=[candidate(senior), candidate(deputy)],
        manual_override=None,
    )

    result = calculate_payroll_lines_from_inputs(
        period,
        uuid.uuid4(),
        [entry(period, senior, work_date), entry(period, deputy, work_date)],
        {senior.id: senior, deputy.id: deputy},
        payroll_settings(work_date, assignment),
    )
    day_by_employee = {line.employee_id: line.components["days"][0] for line in result.lines}

    assert day_by_employee[senior.id]["seniority_allowance_pay"] == 500
    assert day_by_employee[deputy.id]["seniority_allowance_pay"] == 0
    assert day_by_employee[deputy.id]["seniority_allowance_skipped_reason"] == "default_senior"


def test_payroll_chef_both_get_allowance_independently() -> None:
    work_date = date(2026, 6, 1)
    period = make_period(work_date)
    senior = employee(position="Повар", category="category_2", senior=True)
    deputy = employee(position="Повар", category="category_2", deputy=True)

    result = calculate_payroll_lines_from_inputs(
        period,
        uuid.uuid4(),
        [entry(period, senior, work_date), entry(period, deputy, work_date)],
        {senior.id: senior, deputy.id: deputy},
        payroll_settings(work_date),
    )
    day_by_employee = {line.employee_id: line.components["days"][0] for line in result.lines}

    assert day_by_employee[senior.id]["seniority_allowance_pay"] == 600
    assert day_by_employee[deputy.id]["seniority_allowance_pay"] == 400


def test_payroll_cashier_plan_priority_deputy() -> None:
    work_date = date(2026, 6, 1)
    period = make_period(work_date)
    senior = employee(position="Кассир", category="category_3", senior=True)
    deputy = employee(position="Кассир", category="category_3", deputy=True)
    assignment = resolve_cashier_allowance_recipient(
        candidates=[candidate(senior, planned=False), candidate(deputy, planned=True)],
        manual_override=None,
    )

    result = calculate_payroll_lines_from_inputs(
        period,
        uuid.uuid4(),
        [entry(period, senior, work_date), entry(period, deputy, work_date)],
        {senior.id: senior, deputy.id: deputy},
        payroll_settings(work_date, assignment),
    )
    day_by_employee = {line.employee_id: line.components["days"][0] for line in result.lines}

    assert day_by_employee[senior.id]["seniority_allowance_pay"] == 0
    assert day_by_employee[deputy.id]["seniority_allowance_pay"] == 300
    assert day_by_employee[deputy.id]["seniority_allowance_reason"] == "plan_priority"


def test_payroll_cashier_proportional_to_hours() -> None:
    work_date = date(2026, 6, 1)
    period = make_period(work_date)
    senior = employee(position="Кассир", category="category_3", senior=True)
    assignment = resolve_cashier_allowance_recipient(
        candidates=[candidate(senior)],
        manual_override=None,
    )

    result = calculate_payroll_lines_from_inputs(
        period,
        uuid.uuid4(),
        [entry(period, senior, work_date, minutes=360)],
        {senior.id: senior},
        payroll_settings(work_date, assignment),
    )

    assert result.lines[0].components["days"][0]["seniority_allowance_pay"] == 250


async def test_shift_cost_estimate_cashier_uses_resolver() -> None:
    work_date = date(2026, 6, 1)
    schedule_id = uuid.uuid4()
    senior = employee(position="Кассир", category="category_3", senior=True)
    deputy = employee(position="Кассир", category="category_3", deputy=True)
    shift = ScheduledShift(
        id=uuid.uuid4(),
        shift_schedule_id=schedule_id,
        business_date=work_date,
        employee_id=senior.id,
        payroll_role="Администратор",
        station_code="Касса",
        planned_start_at=datetime(2026, 6, 1, 10, tzinfo=UTC),
        planned_end_at=datetime(2026, 6, 1, 22, tzinfo=UTC),
    )
    assignment = resolve_cashier_allowance_recipient(
        candidates=[candidate(senior), candidate(deputy)],
        manual_override=None,
    )

    result = await compute_shift_cost(
        None,  # type: ignore[arg-type]
        shift=shift,
        employee=senior,
        settings=payroll_settings(work_date, assignment),
        daily_revenue_forecast=None,
        same_day_shifts=[(shift, senior)],
    )

    assert result.allowance == Decimal("500.00")
    assert result.components["seniority_allowance_reason"] == "default_senior"

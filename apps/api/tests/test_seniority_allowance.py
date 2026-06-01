from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from app.models import AttendanceEntry, Employee, PayrollPeriod, PayrollSeniorityPremium
from app.schemas.payroll_config import PayrollSeniorityPremiumBase
from app.services.payroll_calculator import calculate_payroll_lines_from_inputs
from app.services.payroll_config import create_seniority_premium_version
from app.services.seniority_allowance_service import (
    SENIORITY_ALLOWANCE_MAP_CONFIG_KEY,
    get_active_allowance_amount,
)


class ScalarResult:
    def __init__(self, items: list[Any]) -> None:
        self.items = items

    def all(self) -> list[Any]:
        return self.items


class AllowanceFakeSession:
    def __init__(self, premiums: list[PayrollSeniorityPremium]) -> None:
        self.premiums = premiums
        self.committed = False

    async def scalars(self, _stmt: Any) -> ScalarResult:
        return ScalarResult(self.premiums)

    async def scalar(self, _stmt: Any) -> Any | None:
        return None

    def add(self, item: Any) -> None:
        self.premiums.append(item)

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        return None

    async def refresh(self, _item: Any) -> None:
        return None


def payroll_settings(work_date: date) -> dict[str, Any]:
    return {
        "payroll.role_category_rates": {
            "Пиццерист": {"category_2": 2200},
            "Администратор": {"category_3": 2000},
        },
        "payroll.category_rules": {
            "2": {"coeff": 7.5, "deposit_target": 15000, "deposit_withholding": 1000},
            "3": {"coeff": 5, "deposit_target": 10000, "deposit_withholding": 1000},
        },
        "payroll.allowances": {"senior": 9999, "deputy_senior": 9999},
        "payroll.weekday_premium": {"amount": 200, "threshold_hours": 8},
        "payroll.fund_rates_by_tenure": [
            {"min_years": 0.5, "rate": 0.05},
            {"min_years": 1.0, "rate": 0.10},
        ],
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


def make_period(work_date: date) -> PayrollPeriod:
    return PayrollPeriod(
        id=uuid.uuid4(),
        period_type="week",
        start_date=work_date,
        end_date=work_date,
        payroll_date=work_date,
        status="open",
    )


def make_employee(
    *,
    position: str = "Повар",
    category: str = "category_2",
    is_senior: bool = False,
    is_deputy_senior: bool = False,
) -> Employee:
    return Employee(
        id=uuid.uuid4(),
        full_name=f"{position} {uuid.uuid4()}",
        iiko_id=f"iiko-{uuid.uuid4()}",
        position=position,
        category=category,
        status="active",
        hire_date=date(2025, 1, 1),
        tenure_started_at=date(2025, 1, 1),
        is_senior=is_senior,
        is_deputy_senior=is_deputy_senior,
    )


def make_entry(
    period: PayrollPeriod,
    employee: Employee,
    work_date: date,
    *,
    minutes: int = 720,
    role: str = "Пиццерист",
) -> AttendanceEntry:
    return AttendanceEntry(
        id=uuid.uuid4(),
        employee_id=employee.id,
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


def premium(
    position: str,
    role: str,
    amount: Decimal,
    *,
    effective_from: date = date(2026, 1, 1),
    effective_to: date | None = None,
) -> PayrollSeniorityPremium:
    return PayrollSeniorityPremium(
        id=uuid.uuid4(),
        position=position,
        role=role,
        amount=amount,
        percent_of_base=None,
        effective_from=effective_from,
        effective_to=effective_to,
    )


async def test_get_allowance_per_position_and_role() -> None:
    session = AllowanceFakeSession(
        [
            premium("Повар", "senior", Decimal("600")),
            premium("Кассир", "senior", Decimal("500")),
        ]
    )

    amount = await get_active_allowance_amount(
        session,  # type: ignore[arg-type]
        position="Повар",
        role="senior",
        on_date=date(2026, 6, 1),
    )

    assert amount == Decimal("600")


def test_calculator_applies_allowance_to_senior_cook() -> None:
    work_date = date(2026, 6, 1)
    period = make_period(work_date)
    employee = make_employee(is_senior=True)

    result = calculate_payroll_lines_from_inputs(
        period,
        uuid.uuid4(),
        [make_entry(period, employee, work_date)],
        {employee.id: employee},
        payroll_settings(work_date),
    )

    assert result.blocking_issues == []
    assert result.lines[0].base_pay == 2800
    assert result.lines[0].components["days"][0]["seniority_allowance_pay"] == 600


def test_calculator_does_not_apply_allowance_to_non_senior() -> None:
    work_date = date(2026, 6, 1)
    period = make_period(work_date)
    employee = make_employee()

    result = calculate_payroll_lines_from_inputs(
        period,
        uuid.uuid4(),
        [make_entry(period, employee, work_date)],
        {employee.id: employee},
        payroll_settings(work_date),
    )

    assert result.blocking_issues == []
    assert result.lines[0].base_pay == 2200
    assert result.lines[0].components["days"][0]["seniority_allowance_pay"] == 0


def test_calculator_uses_position_specific_amount() -> None:
    work_date = date(2026, 6, 1)
    period = make_period(work_date)
    cook = make_employee(is_senior=True)
    cashier = make_employee(position="Кассир", category="category_3", is_senior=True)

    result = calculate_payroll_lines_from_inputs(
        period,
        uuid.uuid4(),
        [
            make_entry(period, cook, work_date, role="Пиццерист"),
            make_entry(period, cashier, work_date, role="Администратор"),
        ],
        {cook.id: cook, cashier.id: cashier},
        payroll_settings(work_date),
    )
    base_by_employee = {line.employee_id: line.base_pay for line in result.lines}

    assert result.blocking_issues == []
    assert base_by_employee[cook.id] == 2800
    assert base_by_employee[cashier.id] == 2500


def test_partial_shift_proportional_allowance() -> None:
    work_date = date(2026, 6, 1)
    period = make_period(work_date)
    employee = make_employee(is_senior=True)

    result = calculate_payroll_lines_from_inputs(
        period,
        uuid.uuid4(),
        [make_entry(period, employee, work_date, minutes=360)],
        {employee.id: employee},
        payroll_settings(work_date),
    )

    assert result.blocking_issues == []
    assert Decimal(str(result.lines[0].base_pay)) == Decimal("1400.00")
    assert result.lines[0].components["days"][0]["seniority_allowance_pay"] == 300


def test_allowance_in_fund_accrual_base() -> None:
    work_date = date(2026, 6, 1)
    period = make_period(work_date)
    employee = make_employee(is_senior=True)

    result = calculate_payroll_lines_from_inputs(
        period,
        uuid.uuid4(),
        [make_entry(period, employee, work_date)],
        {employee.id: employee},
        payroll_settings(work_date),
    )

    assert result.blocking_issues == []
    assert result.lines[0].base_pay == 2800
    assert result.lines[0].fund_accrual == 280


async def test_seniority_premium_versioning() -> None:
    old = premium("Повар", "senior", Decimal("500"))
    session = AllowanceFakeSession([old])

    created = await create_seniority_premium_version(
        session,  # type: ignore[arg-type]
        PayrollSeniorityPremiumBase(
            position="Повар",
            role="senior",
            amount=Decimal("600"),
            effective_from=date(2026, 6, 1),
        ),
    )

    assert old.effective_to == date(2026, 6, 1)
    assert created.amount == Decimal("600")
    assert session.committed is True


def test_db_unique_constraint_blocks_direct_two_active_seniors() -> None:
    indexes = {index.name: index for index in Employee.__table__.indexes}

    assert indexes["uq_employee_active_senior_per_position"].unique is True
    assert indexes["uq_employee_active_deputy_senior_per_position"].unique is True

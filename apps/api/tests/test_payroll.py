from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.v1.routes import payroll as payroll_routes
from app.main import create_app
from app.models import (
    AccumulationFundAccount,
    AttendanceEntry,
    DepositAccount,
    Employee,
    EmployeeRoleAssignment,
    PayrollPeriod,
    PayrollRun,
)
from app.services.attendance_loader import build_attendance_entry
from app.services.payroll_calculator import (
    EMPLOYEE_ASSIGNMENTS_CONFIG_KEY,
    PAYROLL_RATE_CONFIG_KEY,
    calculate_payroll_lines_from_inputs,
)
from app.services.payroll_percent import (
    PercentShift,
    compute_daily_percent_pool,
    distribute_percent_pool,
)
from app.services.payroll_runner import (
    PayrollConflictError,
    apply_deposit_write_offs_to_accounts,
    apply_fund_payouts_if_due,
    compute_next_payroll_period_dates,
    finalize_payroll_run,
)


def payroll_settings(revenue: dict[str, int] | None = None) -> dict[str, Any]:
    return {
        "payroll.role_category_rates": {
            "Пиццерист": {"category_2": 2200, "intern": 2000},
            "Сушист": {"category_2": 2400},
        },
        "payroll.category_rules": {
            "2": {"coeff": 7.5, "deposit_target": 15000, "deposit_withholding": 1000},
            "4": {"coeff": 0, "deposit_target": 7000, "deposit_withholding": 1000},
        },
        "payroll.revenue_percent_tiers": [
            {"from": 50000, "rate": 0.035},
            {"from": 140000, "rate": 0.045},
            {"from": 190000, "rate": 0.055},
            {"from": 550000, "rate": 0.065},
        ],
        "payroll.allowances": {"senior": 500, "deputy_senior": 300},
        "payroll.weekday_premium": {"friday": 200, "saturday": 200},
        "payroll.fund_rates_by_tenure": [
            {"min_years": 0.5, "rate": 0.05},
            {"min_years": 1.0, "rate": 0.10},
            {"min_years": 1.5, "rate": 0.15},
        ],
        "payroll.mock_daily_revenue": revenue or {},
        "payroll.deposit_auto_withholding_enabled": False,
        "payroll.deposit_fund_payment_date": "01-15",
    }


def make_period(
    start: date = date(2026, 5, 19),
    end: date = date(2026, 5, 25),
    payroll_date: date = date(2026, 5, 26),
) -> PayrollPeriod:
    return PayrollPeriod(
        id=uuid.uuid4(),
        period_type="week",
        start_date=start,
        end_date=end,
        payroll_date=payroll_date,
        status="open",
    )


def make_employee(
    *,
    status: str = "active",
    position: str | None = "Пиццерист",
    category: str | None = "category_2",
    default_cooking_station: str | None = None,
    hire_date: date | None = None,
) -> Employee:
    return Employee(
        id=uuid.uuid4(),
        full_name="Payroll Employee",
        iiko_id=f"iiko-{uuid.uuid4()}",
        position=position,
        category=category,
        default_cooking_station=default_cooking_station,
        status=status,
        hire_date=hire_date,
        is_senior=False,
        is_deputy_senior=False,
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
        updated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )


def make_entry(
    period: PayrollPeriod,
    employee: Employee,
    work_date: date,
    minutes: int = 720,
    role: str | None = "Пиццерист",
    station: str | None = None,
    quality_status: str = "ok",
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
        station=station,
        source="manual",
        quality_status=quality_status,
        notes=None,
    )


def test_tuesday_monday_payroll_period_is_computed() -> None:
    start_date, end_date, payroll_date = compute_next_payroll_period_dates(date(2026, 5, 27))

    assert start_date == date(2026, 5, 19)
    assert end_date == date(2026, 5, 25)
    assert payroll_date == date(2026, 5, 26)


def test_open_shift_closes_at_22_msk() -> None:
    period = make_period()
    employee = make_employee()

    entry = build_attendance_entry(
        {"employeeId": employee.iiko_id, "dateFrom": "2026-05-19T11:00:00+03:00"},
        period,
        employee,
    )

    assert entry.ended_at == datetime(2026, 5, 19, 19, 0, tzinfo=UTC)
    assert entry.minutes_worked == 11 * 60
    assert entry.quality_status == "ok"


def test_closed_shift_over_12_hours_requires_quality_review() -> None:
    period = make_period()
    employee = make_employee()

    entry = build_attendance_entry(
        {
            "employeeId": employee.iiko_id,
            "dateFrom": "2026-05-19T08:00:00+03:00",
            "dateTo": "2026-05-19T21:00:00+03:00",
        },
        period,
        employee,
    )

    assert entry.minutes_worked == 13 * 60
    assert entry.quality_status == "quality_review"
    assert "duration_over_12h" in (entry.notes or "")


@pytest.mark.parametrize(
    ("daily_revenue", "expected_pool"),
    [
        (Decimal("50000"), Decimal("1750.00000")),
        (Decimal("140000"), Decimal("6300.00000")),
        (Decimal("550000"), Decimal("35750.00000")),
    ],
)
def test_compute_daily_percent_pool_uses_revenue_tiers(
    daily_revenue: Decimal,
    expected_pool: Decimal,
) -> None:
    assert compute_daily_percent_pool(daily_revenue, date(2026, 5, 19)) == expected_pool


def test_distribute_percent_pool_weights_three_categories_by_coeff_and_hours() -> None:
    distribution = distribute_percent_pool(
        Decimal("6500"),
        [
            PercentShift("employee-1", "category_1", Decimal("12")),
            PercentShift("employee-2", "category_2", Decimal("12")),
            PercentShift("employee-3", "category_3", Decimal("12")),
        ],
    )

    assert distribution == {
        "employee-1": Decimal("3000"),
        "employee-2": Decimal("2000"),
        "employee-3": Decimal("1500"),
    }


def test_distribute_percent_pool_gives_intern_zero() -> None:
    distribution = distribute_percent_pool(
        Decimal("5000"),
        [
            PercentShift("cook", "category_1", Decimal("12")),
            PercentShift("intern", "intern", Decimal("12")),
        ],
    )

    assert distribution["cook"] == Decimal("5000")
    assert distribution["intern"] == Decimal("0")


def test_distribute_percent_pool_excludes_freelancer_weight() -> None:
    distribution = distribute_percent_pool(
        Decimal("5000"),
        [
            PercentShift("cook", "category_1", Decimal("12")),
            PercentShift("freelancer", "freelancer", Decimal("12")),
        ],
    )

    assert distribution["cook"] == Decimal("5000")
    assert distribution["freelancer"] == Decimal("0")


def test_unknown_or_unconfigured_employee_blocks_payroll_and_finalize() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee(status="requires_setup", position=None, category=None)
    entry = make_entry(period, employee, period.start_date, role=None)

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [entry],
        {employee.id: employee},
        payroll_settings(),
    )

    assert result.blocking_issues[0]["type"] == "needs_setup"


def test_employee_position_does_not_backfill_missing_payroll_role() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee(position="Пиццерист", category="category_2")
    entry = make_entry(period, employee, period.start_date, role=None)

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [entry],
        {employee.id: employee},
        payroll_settings(),
    )

    assert result.blocking_issues[0]["type"] == "missing_payroll_role"


def test_fixed_salary_for_full_week_is_calculated() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee()
    entries = [
        make_entry(period, employee, period.start_date.replace(day=19 + offset))
        for offset in range(7)
    ]

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        entries,
        {employee.id: employee},
        payroll_settings(),
    )

    assert result.blocking_issues == []
    assert result.lines[0].base_pay == 15400
    assert result.lines[0].premium == 400
    assert result.lines[0].total_payable == 15800


def test_weekday_premium_applies_on_friday() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee()
    entry = make_entry(period, employee, date(2026, 5, 22))

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [entry],
        {employee.id: employee},
        payroll_settings(),
    )

    assert result.blocking_issues == []
    assert result.lines[0].premium == 200
    assert result.lines[0].total_payable == 2400
    assert result.lines[0].components["days"][0]["weekday_premium"] == 200


def test_weekday_premium_applies_on_saturday() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee()
    entry = make_entry(period, employee, date(2026, 5, 23))

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [entry],
        {employee.id: employee},
        payroll_settings(),
    )

    assert result.blocking_issues == []
    assert result.lines[0].premium == 200
    assert result.lines[0].total_payable == 2400


def test_weekday_premium_does_not_apply_on_wednesday() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee()
    entry = make_entry(period, employee, date(2026, 5, 20))

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [entry],
        {employee.id: employee},
        payroll_settings(),
    )

    assert result.blocking_issues == []
    assert result.lines[0].premium == 0
    assert result.lines[0].total_payable == 2200


def test_weekday_premium_uses_updated_setting_on_next_run() -> None:
    period = make_period()
    employee = make_employee()
    entry = make_entry(period, employee, date(2026, 5, 22))
    settings = payroll_settings()

    first_result = calculate_payroll_lines_from_inputs(
        period,
        uuid.uuid4(),
        [entry],
        {employee.id: employee},
        settings,
    )
    settings["payroll.weekday_premium"] = {"friday": 500, "saturday": 0}
    second_result = calculate_payroll_lines_from_inputs(
        period,
        uuid.uuid4(),
        [entry],
        {employee.id: employee},
        settings,
    )

    assert first_result.lines[0].premium == 200
    assert second_result.lines[0].premium == 500
    assert second_result.lines[0].total_payable == 2700


def test_intern_with_zero_coeff_still_gets_weekday_premium() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee(category="intern")
    entry = make_entry(period, employee, date(2026, 5, 22))

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [entry],
        {employee.id: employee},
        payroll_settings({"2026-05-22": 190000}),
    )

    assert result.blocking_issues == []
    assert result.lines[0].base_pay == 2000
    assert result.lines[0].premium == 200
    assert result.lines[0].percent_pay == 0
    assert result.lines[0].total_payable == 2200


def test_payroll_calculator_prefers_versioned_rate_configuration() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee()
    entry = make_entry(period, employee, period.start_date)
    settings = payroll_settings()
    del settings["payroll.role_category_rates"]
    settings[PAYROLL_RATE_CONFIG_KEY] = [
        {
            "position_group": "Пиццерист",
            "category": "category_2",
            "station": None,
            "rate_type": "daily",
            "amount": Decimal("3100"),
            "effective_from": date(2026, 1, 1),
            "effective_to": None,
        }
    ]

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [entry],
        {employee.id: employee},
        settings,
    )

    assert result.blocking_issues == []
    assert result.lines[0].base_pay == 3100


def test_payroll_calculator_uses_assignment_category_for_shift_station() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee(
        position="Повар",
        category="category_1",
        default_cooking_station="sushi",
    )
    entry = make_entry(period, employee, period.start_date, role="Пиццерист", station="pizza")
    settings = payroll_settings()
    settings["payroll.role_category_rates"] = {"Пиццерист": {"category_2": 2200}}
    settings[EMPLOYEE_ASSIGNMENTS_CONFIG_KEY] = {
        (employee.id, period.start_date): [
            EmployeeRoleAssignment(
                id=uuid.uuid4(),
                employee_id=employee.id,
                payroll_role="sushi",
                category="category_1",
                is_primary=True,
                effective_from=period.start_date,
            ),
            EmployeeRoleAssignment(
                id=uuid.uuid4(),
                employee_id=employee.id,
                payroll_role="pizza",
                category="category_2",
                is_primary=False,
                effective_from=period.start_date,
            ),
        ]
    }

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [entry],
        {employee.id: employee},
        settings,
    )

    assert result.blocking_issues == []
    assert result.lines[0].base_pay == 2200
    assert result.lines[0].components["days"][0]["category"] == "category_2"


def test_freelancer_category_does_not_match_legacy_daily_rate() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee(category="freelancer")
    entry = make_entry(period, employee, period.start_date)
    settings = payroll_settings()
    settings["payroll.role_category_rates"] = {"Пиццерист": {"6": 5000}}

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [entry],
        {employee.id: employee},
        settings,
    )

    assert result.blocking_issues[0]["type"] == "missing_role_category_rate"


def test_percent_from_revenue_uses_settings_revenue_mock() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee()
    entry = make_entry(period, employee, period.start_date)

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [entry],
        {employee.id: employee},
        payroll_settings({period.start_date.isoformat(): 140000}),
    )

    assert result.blocking_issues == []
    assert result.lines[0].percent_pay == 6300
    assert result.lines[0].components["days"][0]["revenue_rate"] == 0.045


def test_fund_is_paid_out_on_january_15_for_previous_year() -> None:
    employee_id = uuid.uuid4()
    account = AccumulationFundAccount(
        id=uuid.uuid4(),
        employee_id=employee_id,
        year=2025,
        accumulated_amount=Decimal("5000"),
        paid_out_amount=Decimal("0"),
        status="active",
    )

    paid = apply_fund_payouts_if_due([account], date(2026, 1, 15))

    assert paid == Decimal("5000")
    assert account.paid_out_amount == Decimal("5000")
    assert account.status == "paid_out"


def test_deposit_write_off_reduces_deposit_balance() -> None:
    employee_id = uuid.uuid4()
    account = DepositAccount(
        id=uuid.uuid4(),
        employee_id=employee_id,
        balance=Decimal("1000"),
        last_updated=datetime(2026, 5, 27, tzinfo=UTC),
    )

    transactions = apply_deposit_write_offs_to_accounts(
        {employee_id: account},
        [{"employee_id": employee_id, "amount": Decimal("300")}],
        uuid.uuid4(),
        datetime(2026, 5, 27, 10, 0, tzinfo=UTC),
    )

    assert account.balance == Decimal("700")
    assert transactions[0].transaction_type == "write_off"
    assert transactions[0].amount == Decimal("300")


def test_finalize_request_manager_returns_403(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app()
    client = TestClient(app)

    async def fake_finalize(*_args, **_kwargs):
        raise AssertionError("manager must not reach finalize service")

    monkeypatch.setattr(payroll_routes, "finalize_payroll_run", fake_finalize)

    response = client.post(
        f"/api/v1/payroll/runs/{uuid.uuid4()}/finalize",
        headers={"X-User-Role": "manager"},
    )

    assert response.status_code == 403
    client.close()


def test_finalize_request_finance_manager_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app()
    client = TestClient(app)
    run_id = uuid.uuid4()

    async def fake_finalize(_session, _run_id):
        return PayrollRun(
            id=run_id,
            period_id=uuid.uuid4(),
            started_at=datetime(2026, 5, 27, tzinfo=UTC),
            finished_at=datetime(2026, 5, 27, 1, tzinfo=UTC),
            status="finalized",
            blocking_issues=[],
            summary={},
        )

    async def fake_get_run(_session, _run_id):
        return {
            "id": run_id,
            "period_id": uuid.uuid4(),
            "started_at": datetime(2026, 5, 27, tzinfo=UTC),
            "finished_at": datetime(2026, 5, 27, 1, tzinfo=UTC),
            "status": "finalized",
            "blocking_issues": [],
            "summary": {},
            "period": None,
        }

    monkeypatch.setattr(payroll_routes, "finalize_payroll_run", fake_finalize)
    monkeypatch.setattr(payroll_routes, "get_run", fake_get_run)

    response = client.post(
        f"/api/v1/payroll/runs/{run_id}/finalize",
        headers={"X-User-Role": "finance_manager"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "finalized"
    client.close()


class FinalizeFakeSession:
    def __init__(self, run: PayrollRun, period: PayrollPeriod) -> None:
        self.run = run
        self.period = period
        self.committed = False

    async def get(self, model, object_id):
        if model is PayrollRun and object_id == self.run.id:
            return self.run
        if model is PayrollPeriod and object_id == self.period.id:
            return self.period
        return None

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _model) -> None:
        return None


async def test_finalize_without_issues_sets_status_finalized() -> None:
    period = make_period()
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=period.id,
        started_at=datetime(2026, 5, 27, tzinfo=UTC),
        finished_at=datetime(2026, 5, 27, 1, tzinfo=UTC),
        status="completed",
        blocking_issues=[],
        summary={},
    )
    session = FinalizeFakeSession(run, period)

    finalized = await finalize_payroll_run(session, run.id)  # type: ignore[arg-type]

    assert finalized.status == "finalized"
    assert period.status == "finalized"
    assert session.committed is True


async def test_finalized_run_cannot_be_finalized_again() -> None:
    period = make_period()
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=period.id,
        started_at=datetime(2026, 5, 27, tzinfo=UTC),
        status="finalized",
        blocking_issues=[],
        summary={},
    )
    session = FinalizeFakeSession(run, period)

    with pytest.raises(PayrollConflictError):
        await finalize_payroll_run(session, run.id)  # type: ignore[arg-type]

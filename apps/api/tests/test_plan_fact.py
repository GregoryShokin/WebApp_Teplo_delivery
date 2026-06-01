from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

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
from app.services import plan_fact_service


class ScalarResult:
    def __init__(self, items: list[Any]) -> None:
        self.items = items

    def all(self) -> list[Any]:
        return self.items


class ExecuteResult(ScalarResult):
    pass


class PlanFactFakeSession:
    def __init__(
        self,
        *,
        schedules: list[ShiftSchedule] | None = None,
        employees: list[Employee] | None = None,
        shifts: list[ScheduledShift] | None = None,
        forecast_runs: list[PayrollForecastRun] | None = None,
        estimates: list[ShiftCostEstimate] | None = None,
        revenue_forecasts: list[RevenueForecast] | None = None,
        periods: list[PayrollPeriod] | None = None,
        payroll_runs: list[PayrollRun] | None = None,
        payroll_lines: list[PayrollLine] | None = None,
        ledger_entries: list[ShiftLedgerEntry] | None = None,
        setting: AppSetting | None = None,
    ) -> None:
        self.schedules = {item.id: item for item in schedules or []}
        self.employees = {item.id: item for item in employees or []}
        self.shifts = shifts or []
        self.forecast_runs = forecast_runs or []
        self.estimates = estimates or []
        self.revenue_forecasts = revenue_forecasts or []
        self.periods = periods or []
        self.payroll_runs = payroll_runs or []
        self.payroll_lines = payroll_lines or []
        self.ledger_entries = ledger_entries or []
        self.setting = setting

    async def get(self, model: Any, item_id: uuid.UUID) -> Any | None:
        if model is ShiftSchedule:
            return self.schedules.get(item_id)
        return None

    async def execute(self, query: Any) -> ExecuteResult:
        entities = query_entities(query)
        if entities[:2] == [ScheduledShift, Employee]:
            return ExecuteResult(
                [
                    (shift, self.employees[shift.employee_id])
                    for shift in self.shifts
                    if shift.employee_id in self.employees
                ]
            )
        return ExecuteResult([])

    async def scalars(self, query: Any) -> ScalarResult:
        entity = query_entity(query)
        values: dict[Any, list[Any]] = {
            PayrollForecastRun: self.forecast_runs,
            ShiftCostEstimate: self.estimates,
            RevenueForecast: self.revenue_forecasts,
            PayrollPeriod: self.periods,
            PayrollRun: self.payroll_runs,
            PayrollLine: self.payroll_lines,
            ShiftLedgerEntry: self.ledger_entries,
            Employee: list(self.employees.values()),
            AppSetting: [self.setting] if self.setting is not None else [],
        }
        return ScalarResult(values.get(entity, []))

    async def scalar(self, query: Any) -> Any | None:
        if query_entity(query) is AppSetting:
            return self.setting
        return None


def query_entity(query: Any) -> Any | None:
    entities = query_entities(query)
    return entities[0] if entities else None


def query_entities(query: Any) -> list[Any]:
    return [
        description.get("entity")
        for description in (getattr(query, "column_descriptions", None) or [])
    ]


@pytest.fixture(autouse=True)
def stub_iiko(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch_daily_revenue(
        *_args: Any,
        **_kwargs: Any,
    ) -> dict[date, Decimal]:
        return {}

    monkeypatch.setattr(plan_fact_service, "fetch_daily_revenue", fake_fetch_daily_revenue)


def make_schedule(
    date_start: date = date(2026, 6, 1),
    date_end: date = date(2026, 6, 1),
) -> ShiftSchedule:
    return ShiftSchedule(
        id=uuid.uuid4(),
        date_start=date_start,
        date_end=date_end,
        status="published",
    )


def make_employee(full_name: str = "Иванов Иван", position: str = "Повар") -> Employee:
    return Employee(
        id=uuid.uuid4(),
        full_name=full_name,
        iiko_id=f"iiko-{uuid.uuid4()}",
        position=position,
        status="active",
    )


def make_shift(
    schedule: ShiftSchedule,
    employee: Employee,
    business_date: date,
    *,
    hours: int = 12,
) -> ScheduledShift:
    start = datetime.combine(business_date, datetime.min.time(), tzinfo=UTC).replace(hour=8)
    end = start + timedelta(hours=hours)
    return ScheduledShift(
        id=uuid.uuid4(),
        shift_schedule_id=schedule.id,
        business_date=business_date,
        employee_id=employee.id,
        payroll_role=employee.position,
        station_code="Пицца",
        planned_start_at=start,
        planned_end_at=end,
    )


def make_forecast_run(
    schedule: ShiftSchedule,
    *,
    status: str = "completed",
    run_at: datetime | None = None,
) -> PayrollForecastRun:
    return PayrollForecastRun(
        id=uuid.uuid4(),
        shift_schedule_id=schedule.id,
        run_at=run_at or datetime(2026, 6, 1, 10, tzinfo=UTC),
        status=status,
    )


def make_estimate(
    run: PayrollForecastRun,
    shift: ScheduledShift,
    *,
    cost: Decimal = Decimal("1000"),
) -> ShiftCostEstimate:
    return ShiftCostEstimate(
        id=uuid.uuid4(),
        forecast_run_id=run.id,
        scheduled_shift_id=shift.id,
        business_date=shift.business_date,
        employee_id=shift.employee_id,
        planned_hours=Decimal("12.00"),
        base_salary_estimate=cost,
        weekday_premium_estimate=Decimal("0"),
        allowance_estimate=Decimal("0"),
        revenue_percent_estimate=Decimal("0"),
        fund_accrual_estimate=Decimal("0"),
        total_cost_estimate=cost,
        quality_status="ok",
        quality_reasons=[],
        breakdown={},
    )


def make_revenue_forecast(business_date: date, amount: Decimal) -> RevenueForecast:
    return RevenueForecast(
        id=uuid.uuid4(),
        business_date=business_date,
        weekday=business_date.weekday(),
        forecast_amount=amount,
        quality_status="ok",
    )


def make_period(start: date, end: date) -> PayrollPeriod:
    return PayrollPeriod(
        id=uuid.uuid4(),
        period_type="week",
        start_date=start,
        end_date=end,
        payroll_date=end + timedelta(days=1),
        status="open",
    )


def make_payroll_run(
    period: PayrollPeriod,
    *,
    status: str = "completed",
    started_at: datetime | None = None,
) -> PayrollRun:
    return PayrollRun(
        id=uuid.uuid4(),
        period_id=period.id,
        started_at=started_at or datetime(2026, 6, 1, 12, tzinfo=UTC),
        status=status,
        blocking_issues=[],
        summary={},
    )


def make_line(
    run: PayrollRun,
    employee: Employee,
    *,
    base_pay: Decimal = Decimal("1000"),
    premium: Decimal = Decimal("0"),
    percent_pay: Decimal = Decimal("0"),
) -> PayrollLine:
    return PayrollLine(
        id=uuid.uuid4(),
        run_id=run.id,
        employee_id=employee.id,
        role=employee.position,
        base_pay=base_pay,
        premium=premium,
        percent_pay=percent_pay,
        vacation_pay=Decimal("0"),
        fund_accrual=Decimal("0"),
        deduction=Decimal("0"),
        total_payable=base_pay + premium + percent_pay,
        components={},
    )


def make_ledger(
    employee: Employee,
    work_date: date,
    *,
    minutes: int = 720,
    closed: bool = True,
) -> ShiftLedgerEntry:
    opened_at = datetime.combine(work_date, datetime.min.time(), tzinfo=UTC).replace(hour=8)
    return ShiftLedgerEntry(
        id=uuid.uuid4(),
        work_date=work_date,
        employee_id=employee.id,
        payroll_role=employee.position,
        category="category_2",
        source="manual_correction",
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=minutes) if closed else None,
        is_resolved=True,
    )


def make_setting(value: str = "5") -> AppSetting:
    return AppSetting(
        id=uuid.uuid4(),
        key=plan_fact_service.PLAN_FACT_WARNING_THRESHOLD_KEY,
        value=value,
        value_type="number",
        category="schedule",
        display_name="Порог",
        widget_type="percent",
    )


def days(start: date, count: int) -> list[date]:
    return [start + timedelta(days=index) for index in range(count)]


async def test_plan_fact_no_payroll_runs_returns_none_actual() -> None:
    sched = make_schedule()
    employee = make_employee()
    shift = make_shift(sched, employee, sched.date_start)
    session = PlanFactFakeSession(
        schedules=[sched],
        employees=[employee],
        shifts=[shift],
    )

    summary = await plan_fact_service.compute_plan_fact(session, sched.id)  # type: ignore[arg-type]

    assert summary.fact_availability == "none"
    assert summary.actual is None
    assert summary.by_employee[0].deviation_status == "plan_no_fact"


async def test_plan_fact_full_coverage() -> None:
    sched = make_schedule(date(2026, 6, 1), date(2026, 6, 2))
    employee = make_employee()
    shifts = [make_shift(sched, employee, day) for day in days(sched.date_start, 2)]
    forecast_run = make_forecast_run(sched)
    estimates = [make_estimate(forecast_run, shift, cost=Decimal("100")) for shift in shifts]
    period = make_period(sched.date_start, sched.date_end)
    payroll_run = make_payroll_run(period)
    line = make_line(payroll_run, employee, base_pay=Decimal("200"))
    ledger = [make_ledger(employee, day) for day in days(sched.date_start, 2)]
    session = PlanFactFakeSession(
        schedules=[sched],
        employees=[employee],
        shifts=shifts,
        forecast_runs=[forecast_run],
        estimates=estimates,
        revenue_forecasts=[
            make_revenue_forecast(sched.date_start, Decimal("1000")),
            make_revenue_forecast(sched.date_end, Decimal("1000")),
        ],
        periods=[period],
        payroll_runs=[payroll_run],
        payroll_lines=[line],
        ledger_entries=ledger,
    )

    summary = await plan_fact_service.compute_plan_fact(session, sched.id)  # type: ignore[arg-type]

    assert summary.fact_availability == "full"
    assert summary.planned["total_shifts"] == 2
    assert summary.actual is not None
    assert summary.actual["total_hours"] == Decimal("24.00")
    assert summary.actual["total_cost"] == Decimal("200.00")
    assert summary.by_date[0].actual_cost == Decimal("100.00")
    assert summary.by_employee[0].actual_shifts == 2


async def test_plan_fact_partial_coverage() -> None:
    sched = make_schedule(date(2026, 6, 1), date(2026, 6, 10))
    employee = make_employee()
    shifts = [make_shift(sched, employee, day) for day in days(sched.date_start, 10)]
    covered_period = make_period(date(2026, 6, 1), date(2026, 6, 7))
    uncovered_period = make_period(date(2026, 6, 8), date(2026, 6, 10))
    payroll_run = make_payroll_run(covered_period)
    session = PlanFactFakeSession(
        schedules=[sched],
        employees=[employee],
        shifts=shifts,
        periods=[covered_period, uncovered_period],
        payroll_runs=[payroll_run],
    )

    summary = await plan_fact_service.compute_plan_fact(session, sched.id)  # type: ignore[arg-type]

    assert summary.fact_availability == "partial"
    assert summary.covered_dates == days(date(2026, 6, 1), 7)


async def test_plan_fact_deviation_within_threshold() -> None:
    summary = await compute_hours_case(actual_minutes=[720] * 11 + [600])

    assert summary.by_employee[0].planned_hours == Decimal("144.00")
    assert summary.by_employee[0].actual_hours == Decimal("142.00")
    assert summary.by_employee[0].hours_deviation_pct == Decimal("-1.39")
    assert summary.by_employee[0].deviation_status == "within_threshold"


async def test_plan_fact_deviation_over_threshold() -> None:
    summary = await compute_hours_case(actual_minutes=[720] * 10 + [600] + [0])

    assert summary.by_employee[0].actual_hours == Decimal("130.00")
    assert summary.by_employee[0].hours_deviation_pct == Decimal("-9.72")
    assert summary.by_employee[0].deviation_status == "over_threshold"


async def test_plan_fact_plan_no_fact_row() -> None:
    sched = make_schedule()
    employee = make_employee()
    shift = make_shift(sched, employee, sched.date_start)
    session = PlanFactFakeSession(schedules=[sched], employees=[employee], shifts=[shift])

    summary = await plan_fact_service.compute_plan_fact(session, sched.id)  # type: ignore[arg-type]

    assert summary.by_employee[0].deviation_status == "plan_no_fact"
    assert summary.by_employee[0].hours_deviation_pct is None


async def test_plan_fact_fact_no_plan_row() -> None:
    sched = make_schedule()
    employee = make_employee()
    period = make_period(sched.date_start, sched.date_end)
    payroll_run = make_payroll_run(period)
    session = PlanFactFakeSession(
        schedules=[sched],
        employees=[employee],
        periods=[period],
        payroll_runs=[payroll_run],
        payroll_lines=[make_line(payroll_run, employee)],
        ledger_entries=[make_ledger(employee, sched.date_start)],
    )

    summary = await plan_fact_service.compute_plan_fact(session, sched.id)  # type: ignore[arg-type]

    assert summary.by_employee[0].planned_shifts == 0
    assert summary.by_employee[0].actual_shifts == 1
    assert summary.by_employee[0].deviation_status == "fact_no_plan"


async def test_plan_fact_uses_latest_completed_payroll_run() -> None:
    sched = make_schedule()
    employee = make_employee()
    period = make_period(sched.date_start, sched.date_end)
    completed = make_payroll_run(
        period,
        status="completed",
        started_at=datetime(2026, 6, 1, 10, tzinfo=UTC),
    )
    draft = make_payroll_run(
        period,
        status="draft",
        started_at=datetime(2026, 6, 1, 12, tzinfo=UTC),
    )
    session = PlanFactFakeSession(
        schedules=[sched],
        employees=[employee],
        periods=[period],
        payroll_runs=[draft, completed],
        payroll_lines=[
            make_line(draft, employee, base_pay=Decimal("900")),
            make_line(completed, employee, base_pay=Decimal("100")),
        ],
        ledger_entries=[make_ledger(employee, sched.date_start)],
    )

    summary = await plan_fact_service.compute_plan_fact(session, sched.id)  # type: ignore[arg-type]

    assert summary.actual is not None
    assert summary.actual["total_cost"] == Decimal("100.00")


async def test_plan_fact_skips_unclosed_ledger_entries() -> None:
    sched = make_schedule()
    employee = make_employee()
    period = make_period(sched.date_start, sched.date_end)
    payroll_run = make_payroll_run(period)
    session = PlanFactFakeSession(
        schedules=[sched],
        employees=[employee],
        periods=[period],
        payroll_runs=[payroll_run],
        ledger_entries=[make_ledger(employee, sched.date_start, closed=False)],
    )

    summary = await plan_fact_service.compute_plan_fact(session, sched.id)  # type: ignore[arg-type]

    assert summary.fact_availability == "full"
    assert summary.by_date[0].actual_hours is None


async def test_plan_fact_ledger_cap_720_minutes() -> None:
    sched = make_schedule()
    employee = make_employee()
    period = make_period(sched.date_start, sched.date_end)
    payroll_run = make_payroll_run(period)
    session = PlanFactFakeSession(
        schedules=[sched],
        employees=[employee],
        periods=[period],
        payroll_runs=[payroll_run],
        ledger_entries=[make_ledger(employee, sched.date_start, minutes=840)],
    )

    summary = await plan_fact_service.compute_plan_fact(session, sched.id)  # type: ignore[arg-type]

    assert summary.by_date[0].actual_hours == Decimal("12.00")


async def test_plan_fact_warning_threshold_from_settings() -> None:
    sched = make_schedule()
    session = PlanFactFakeSession(schedules=[sched], setting=make_setting("5"))

    summary = await plan_fact_service.compute_plan_fact(session, sched.id)  # type: ignore[arg-type]

    assert summary.warning_threshold_pct == Decimal("5")


async def test_plan_fact_revenue_from_iiko_actual(monkeypatch: pytest.MonkeyPatch) -> None:
    sched = make_schedule(date(2026, 6, 1), date(2026, 6, 2))
    employee = make_employee()
    period = make_period(sched.date_start, sched.date_end)
    payroll_run = make_payroll_run(period)

    async def fake_fetch_daily_revenue(
        *_args: Any,
        **_kwargs: Any,
    ) -> dict[date, Decimal]:
        return {
            sched.date_start: Decimal("100"),
            sched.date_end: Decimal("200"),
        }

    monkeypatch.setattr(plan_fact_service, "fetch_daily_revenue", fake_fetch_daily_revenue)
    session = PlanFactFakeSession(
        schedules=[sched],
        employees=[employee],
        periods=[period],
        payroll_runs=[payroll_run],
        ledger_entries=[make_ledger(employee, sched.date_start)],
    )

    summary = await plan_fact_service.compute_plan_fact(session, sched.id)  # type: ignore[arg-type]

    assert summary.actual is not None
    assert summary.actual["total_revenue"] == Decimal("300.00")


async def test_plan_fact_cost_day_distribution_proportional_to_hours() -> None:
    sched = make_schedule(date(2026, 6, 1), date(2026, 6, 2))
    employee = make_employee()
    period = make_period(sched.date_start, sched.date_end)
    payroll_run = make_payroll_run(period)
    session = PlanFactFakeSession(
        schedules=[sched],
        employees=[employee],
        periods=[period],
        payroll_runs=[payroll_run],
        payroll_lines=[make_line(payroll_run, employee, base_pay=Decimal("10000"))],
        ledger_entries=[
            make_ledger(employee, sched.date_start, minutes=720),
            make_ledger(employee, sched.date_end, minutes=720),
        ],
    )

    summary = await plan_fact_service.compute_plan_fact(session, sched.id)  # type: ignore[arg-type]

    assert [row.actual_cost for row in summary.by_date] == [
        Decimal("5000.00"),
        Decimal("5000.00"),
    ]


async def compute_hours_case(
    *,
    actual_minutes: list[int],
) -> plan_fact_service.PlanFactSummary:
    sched = make_schedule(date(2026, 6, 1), date(2026, 6, 12))
    employee = make_employee()
    schedule_days = days(sched.date_start, 12)
    shifts = [make_shift(sched, employee, day) for day in schedule_days]
    period = make_period(sched.date_start, sched.date_end)
    payroll_run = make_payroll_run(period)
    ledger = [
        make_ledger(employee, day, minutes=minutes)
        for day, minutes in zip(schedule_days, actual_minutes, strict=True)
        if minutes > 0
    ]
    session = PlanFactFakeSession(
        schedules=[sched],
        employees=[employee],
        shifts=shifts,
        periods=[period],
        payroll_runs=[payroll_run],
        ledger_entries=ledger,
    )
    return await plan_fact_service.compute_plan_fact(session, sched.id)  # type: ignore[arg-type]

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.api.deps import CurrentActor
from app.models import (
    AppSetting,
    Employee,
    PayrollForecastRun,
    RevenueForecast,
    ScheduledShift,
    ShiftCostEstimate,
    ShiftSchedule,
)
from app.services import payroll_forecast_run_service
from app.services.payroll_calculator import (
    CATEGORY_COEFFICIENT_CONFIG_KEY,
    PAYROLL_RATE_CONFIG_KEY,
)
from app.services.payroll_percent import REVENUE_TIER_CONFIG_KEY
from app.services.shift_cost_estimate_service import compute_shift_cost


class ScalarResult:
    def __init__(self, items: list[Any]) -> None:
        self.items = items

    def all(self) -> list[Any]:
        return self.items


class ForecastRunFakeSession:
    def __init__(
        self,
        *,
        schedule: ShiftSchedule | None = None,
        previous_runs: list[PayrollForecastRun] | None = None,
        setting: AppSetting | None = None,
    ) -> None:
        self.schedule = schedule
        self.runs = {run.id: run for run in previous_runs or []}
        self.estimates: list[ShiftCostEstimate] = []
        self.setting = setting
        self.last_added_run_id: uuid.UUID | None = None
        self.committed = False

    async def get(self, model: Any, item_id: uuid.UUID) -> Any | None:
        if model is ShiftSchedule:
            return self.schedule if self.schedule and self.schedule.id == item_id else None
        if model is PayrollForecastRun:
            return self.runs.get(item_id)
        return None

    def add(self, item: Any) -> None:
        if isinstance(item, PayrollForecastRun):
            self.runs[item.id] = item
            self.last_added_run_id = item.id
        elif isinstance(item, ShiftCostEstimate):
            self.estimates.append(item)

    async def flush(self) -> None:
        return None

    async def execute(self, _stmt: Any) -> ScalarResult:
        for run in self.runs.values():
            if run.id != self.last_added_run_id and run.status == "completed":
                run.status = "superseded"
        return ScalarResult([])

    async def scalars(self, _stmt: Any) -> ScalarResult:
        return ScalarResult(list(self.runs.values()))

    async def scalar(self, _stmt: Any) -> Any | None:
        return self.setting

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _item: Any) -> None:
        return None


def actor() -> CurrentActor:
    return CurrentActor(roles=frozenset({"finance_manager"}), user_id=uuid.uuid4())


def make_employee(
    *,
    position: str = "Повар",
    category: str | None = "category_2",
    full_name: str = "Иванов Иван",
    hire_date: date | None = date(2025, 1, 1),
) -> Employee:
    return Employee(
        id=uuid.uuid4(),
        full_name=full_name,
        iiko_id=f"iiko-{uuid.uuid4()}",
        position=position,
        category=category,
        status="active",
        hire_date=hire_date,
        tenure_started_at=hire_date,
        is_senior=False,
        is_deputy_senior=False,
    )


def make_shift(
    schedule_id: uuid.UUID,
    employee: Employee,
    *,
    business_date: date = date(2026, 6, 1),
    start_hour: int = 10,
    end_hour: int = 22,
    station_code: str | None = "Пицца",
) -> ScheduledShift:
    start = datetime.combine(business_date, datetime.min.time(), tzinfo=UTC).replace(
        hour=start_hour
    )
    end_date = business_date if end_hour > start_hour else business_date + timedelta(days=1)
    end = datetime.combine(end_date, datetime.min.time(), tzinfo=UTC).replace(hour=end_hour)
    return ScheduledShift(
        id=uuid.uuid4(),
        shift_schedule_id=schedule_id,
        business_date=business_date,
        employee_id=employee.id,
        payroll_role=employee.position,
        station_code=station_code,
        planned_start_at=start,
        planned_end_at=end,
    )


def make_settings(*, pizza_rate: Decimal | None = Decimal("3600")) -> dict[str, Any]:
    rates = []
    if pizza_rate is not None:
        rates.append(
            {
                "position_group": "Пиццерист",
                "category": "category_2",
                "station": "pizza",
                "rate_type": "daily",
                "amount": pizza_rate,
                "effective_from": date(2026, 1, 1),
                "effective_to": None,
            }
        )
    return {
        PAYROLL_RATE_CONFIG_KEY: rates
        + [
            {
                "position_group": "Администратор",
                "category": "category_3",
                "station": None,
                "rate_type": "daily",
                "amount": Decimal("2400"),
                "effective_from": date(2026, 1, 1),
                "effective_to": None,
            }
        ],
        REVENUE_TIER_CONFIG_KEY: [
            {
                "min_revenue": Decimal("0"),
                "max_revenue": None,
                "rate_percent": Decimal("0.04000"),
                "effective_from": date(2026, 1, 1),
                "effective_to": None,
            }
        ],
        CATEGORY_COEFFICIENT_CONFIG_KEY: [
            {
                "category": "category_2",
                "coefficient": Decimal("2.000"),
                "effective_from": date(2026, 1, 1),
                "effective_to": None,
            },
            {
                "category": "category_3",
                "coefficient": Decimal("1.000"),
                "effective_from": date(2026, 1, 1),
                "effective_to": None,
            },
        ],
        "payroll.allowances": {"senior": 500, "deputy_senior": 300},
        "payroll.weekday_premium": {"amount": 200, "threshold_hours": 8},
        "payroll.fund_rates_by_tenure": [{"min_months": 0, "rate": 0.05}],
    }


async def price_shift(
    shift: ScheduledShift,
    employee: Employee,
    *,
    settings: dict[str, Any] | None = None,
    forecast: Decimal | None = Decimal("100000"),
    same_day: list[tuple[ScheduledShift, Employee]] | None = None,
):
    return await compute_shift_cost(
        None,  # type: ignore[arg-type]
        shift=shift,
        employee=employee,
        settings=settings or make_settings(),
        daily_revenue_forecast=forecast,
        same_day_shifts=same_day or [(shift, employee)],
    )


async def test_compute_shift_cost_basic() -> None:
    employee = make_employee()
    shift = make_shift(uuid.uuid4(), employee)

    result = await price_shift(shift, employee)

    assert result.base_salary == Decimal("3600.00")
    assert result.revenue_percent == Decimal("4000.00")
    assert result.total == Decimal("7600.00")
    assert result.quality_status == "ok"


async def test_compute_with_weekday_premium() -> None:
    employee = make_employee()
    shift = make_shift(uuid.uuid4(), employee, business_date=date(2026, 6, 5))

    result = await price_shift(shift, employee)

    assert result.weekday_premium == Decimal("200.00")
    assert result.total == Decimal("7800.00")


async def test_compute_no_rate_flags_review() -> None:
    employee = make_employee()
    shift = make_shift(uuid.uuid4(), employee)

    result = await price_shift(shift, employee, settings=make_settings(pizza_rate=None))

    assert result.base_salary == Decimal("0.00")
    assert result.quality_status == "requires_review"
    assert "no_rate" in result.quality_reasons


async def test_compute_no_forecast_flags_review() -> None:
    employee = make_employee()
    shift = make_shift(uuid.uuid4(), employee)

    result = await price_shift(shift, employee, forecast=None)

    assert result.revenue_percent == Decimal("0.00")
    assert result.quality_status == "requires_review"
    assert "forecast_missing" in result.quality_reasons


async def test_compute_overnight_shift_flags_review() -> None:
    employee = make_employee()
    shift = make_shift(uuid.uuid4(), employee, start_hour=22, end_hour=6)

    result = await price_shift(shift, employee)

    assert result.planned_hours == Decimal("8.00")
    assert result.base_salary == Decimal("2400.00")
    assert "overnight_shift" in result.quality_reasons


async def test_revenue_percent_distributed_by_weight() -> None:
    schedule_id = uuid.uuid4()
    cook_1 = make_employee(category="category_2", full_name="Повар 1")
    cook_2 = make_employee(category="category_2", full_name="Повар 2")
    cashier = make_employee(position="Кассир", category="category_3", full_name="Кассир")
    shifts = [
        make_shift(schedule_id, cook_1),
        make_shift(schedule_id, cook_2),
        make_shift(schedule_id, cashier, end_hour=16, station_code="Касса"),
    ]
    same_day = list(zip(shifts, [cook_1, cook_2, cashier], strict=True))

    result = await price_shift(shifts[0], cook_1, same_day=same_day)

    assert result.revenue_percent == Decimal("1777.00")


async def test_revenue_percent_zero_when_only_review_shifts_in_day() -> None:
    employee = make_employee()
    shift = make_shift(uuid.uuid4(), employee)

    result = await price_shift(shift, employee, settings=make_settings(pizza_rate=None))

    assert result.quality_status == "requires_review"
    assert result.revenue_percent == Decimal("4000.00")


async def test_fund_accrual_displayed_not_summed() -> None:
    employee = make_employee()
    shift = make_shift(uuid.uuid4(), employee)

    result = await price_shift(shift, employee)

    assert result.fund_accrual == Decimal("180.00")
    assert result.total == result.base_salary + result.weekday_premium + result.revenue_percent


async def test_create_run_aggregates_totals(monkeypatch: pytest.MonkeyPatch) -> None:
    sched = ShiftSchedule(
        id=uuid.uuid4(),
        date_start=date(2026, 6, 1),
        date_end=date(2026, 6, 7),
        status="draft",
    )
    employees = [make_employee(full_name=f"Сотрудник {index}") for index in range(87)]
    shift_rows = [(make_shift(sched.id, employee), employee) for employee in employees]
    forecast = RevenueForecast(
        id=uuid.uuid4(),
        business_date=date(2026, 6, 1),
        weekday=0,
        forecast_amount=Decimal("100000"),
        quality_status="ok",
    )
    session = ForecastRunFakeSession(schedule=sched)

    monkeypatch.setattr(payroll_forecast_run_service, "_load_shift_rows", _returning(shift_rows))
    monkeypatch.setattr(
        payroll_forecast_run_service,
        "_load_pricing_settings",
        _returning(make_settings()),
    )
    monkeypatch.setattr(
        payroll_forecast_run_service,
        "get_forecasts_in_range",
        _returning([forecast]),
    )

    run = await payroll_forecast_run_service.create_forecast_run(
        session,  # type: ignore[arg-type]
        shift_schedule_id=sched.id,
        actor=actor(),
    )

    assert run.shifts_total == 87
    assert run.total_shift_cost_estimate == sum(
        (estimate.total_cost_estimate for estimate in session.estimates),
        Decimal("0"),
    )
    assert run.fot_to_revenue_pct == (run.total_shift_cost_estimate / Decimal("100000") * 100).quantize(
        Decimal("0.00001")
    )


async def test_create_run_supersedes_previous(monkeypatch: pytest.MonkeyPatch) -> None:
    sched = ShiftSchedule(
        id=uuid.uuid4(),
        date_start=date(2026, 6, 1),
        date_end=date(2026, 6, 7),
        status="draft",
    )
    previous = PayrollForecastRun(
        id=uuid.uuid4(),
        shift_schedule_id=sched.id,
        status="completed",
        run_at=datetime(2026, 6, 1, 9, tzinfo=UTC),
    )
    employee = make_employee()
    shift_rows = [(make_shift(sched.id, employee), employee)]
    session = ForecastRunFakeSession(schedule=sched, previous_runs=[previous])

    monkeypatch.setattr(payroll_forecast_run_service, "_load_shift_rows", _returning(shift_rows))
    monkeypatch.setattr(
        payroll_forecast_run_service,
        "_load_pricing_settings",
        _returning(make_settings()),
    )
    monkeypatch.setattr(payroll_forecast_run_service, "get_forecasts_in_range", _returning([]))

    run = await payroll_forecast_run_service.create_forecast_run(
        session,  # type: ignore[arg-type]
        shift_schedule_id=sched.id,
        actor=actor(),
    )

    assert previous.status == "superseded"
    assert run.status == "completed"


async def test_latest_returns_completed_only() -> None:
    schedule_id = uuid.uuid4()
    completed = PayrollForecastRun(
        id=uuid.uuid4(),
        shift_schedule_id=schedule_id,
        status="completed",
        run_at=datetime(2026, 6, 1, 12, tzinfo=UTC),
    )
    superseded = PayrollForecastRun(
        id=uuid.uuid4(),
        shift_schedule_id=schedule_id,
        status="superseded",
        run_at=datetime(2026, 6, 1, 13, tzinfo=UTC),
    )
    session = ForecastRunFakeSession(previous_runs=[superseded, completed])

    latest = await payroll_forecast_run_service.get_latest_run(
        session,  # type: ignore[arg-type]
        schedule_id,
    )

    assert latest is completed


async def test_warning_threshold_from_settings() -> None:
    setting = AppSetting(
        id=uuid.uuid4(),
        key="schedule.fot_warning_threshold_pct",
        value=30,
        value_type="number",
        category="schedule",
        display_name="Порог",
        widget_type="percent",
    )
    session = ForecastRunFakeSession(setting=setting)

    threshold = await payroll_forecast_run_service.get_fot_warning_threshold_pct(
        session  # type: ignore[arg-type]
    )

    assert threshold == Decimal("30")


def _returning(value: Any):
    async def fake(*_args: Any, **_kwargs: Any) -> Any:
        return value

    return fake

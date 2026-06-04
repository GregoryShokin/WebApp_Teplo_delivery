from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.deps import CurrentActor
from app.api.v1.routes import payroll as payroll_routes
from app.api.v1.routes import payroll_adjustments as payroll_adjustment_routes
from app.api.v1.routes import shifts as shift_routes
from app.db.session import get_session
from app.main import create_app
from app.models import (
    AccumulationFundAccount,
    AccumulationFundTransaction,
    AgentAction,
    AppSetting,
    AppSettingHistory,
    AttendanceEntry,
    DeferredAuditCharge,
    DepositAccount,
    DepositTransaction,
    Employee,
    EmployeeRoleAssignment,
    InventoryAudit,
    PayrollAdjustment,
    PayrollAdjustmentCategory,
    PayrollLine,
    PayrollPeriod,
    PayrollRun,
    ShiftLedgerEntry,
)
from app.schemas.payroll import DeferredChargeCreate
from app.services import shift_ledger as shift_ledger_service
from app.services.accumulation_fund_service import forfeit_active_fund_on_dismiss
from app.services.attendance_loader import (
    PAYROLL_TARGET_POSITIONS,
    build_attendance_entry,
    load_attendance_entries,
)
from app.services.deferred_audit_charge_service import (
    cancel_deferred_charge,
    create_deferred_charge,
)
from app.services.payroll_adjustment_service import PayrollAdjustmentLockedError
from app.services.payroll_calculator import (
    DAILY_REVENUE_CONFIG_KEY,
    EMPLOYEE_ALLOWANCES_CONFIG_KEY,
    EMPLOYEE_ASSIGNMENTS_CONFIG_KEY,
    PAYROLL_ADJUSTMENTS_CONFIG_KEY,
    PAYROLL_RATE_CONFIG_KEY,
    SHIFT_LEDGER_CONFIG_KEY,
    VACATION_DAILY_AMOUNT_CONFIG_KEY,
    VACATION_DAYS_CONFIG_KEY,
    _fund_rate_for_months,
    calculate_payroll_lines_from_inputs,
    deposit_withholding,
    employee_deposit_target,
    fund_accrual_for_day,
    tenure_months_on,
)
from app.services.payroll_percent import (
    PercentShift,
    compute_daily_percent_pool,
    distribute_percent_pool,
)
from app.services.payroll_runner import (
    PayrollConflictError,
    accrue_fund,
    apply_deposit_write_offs_to_accounts,
    apply_fund_payouts_if_due,
    compute_next_payroll_period_dates,
    ensure_daily_revenue_cached,
    finalize_payroll_run,
    line_deposit_overrides_from_lines,
    payout_previous_year_fund_if_due,
    run_payroll,
)
from app.services.seniority_allowance_service import SENIORITY_ALLOWANCE_MAP_CONFIG_KEY
from app.services.shift_ledger import AttendanceSnapshot, LedgerAssignment


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
        "payroll.weekday_premium": {"amount": 200, "threshold_hours": 8},
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
    position: str | None = "Повар",
    category: str | None = "category_2",
    default_cooking_station: str | None = None,
    hire_date: date | None = None,
    tenure_started_at: date | None = None,
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
        tenure_started_at=tenure_started_at,
        is_senior=False,
        is_deputy_senior=False,
        pin_hash="hashed-pin",
        pin_set_at=datetime(2026, 5, 1, tzinfo=UTC),
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
        updated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )


def make_role_assignment(
    employee_id: uuid.UUID,
    payroll_role: str,
    category: str,
    *,
    is_primary: bool = False,
    is_substitute: bool = False,
    effective_from: date = date(2026, 1, 1),
    effective_to: date | None = None,
) -> EmployeeRoleAssignment:
    return EmployeeRoleAssignment(
        id=uuid.uuid4(),
        employee_id=employee_id,
        payroll_role=payroll_role,
        category=category,
        is_primary=is_primary,
        is_substitute=is_substitute,
        effective_from=effective_from,
        effective_to=effective_to,
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


def make_payroll_line(
    run_id: uuid.UUID,
    employee_id: uuid.UUID,
    *,
    components: dict[str, Any] | None = None,
) -> PayrollLine:
    return PayrollLine(
        id=uuid.uuid4(),
        run_id=run_id,
        employee_id=employee_id,
        role="Пиццерист",
        base_pay=Decimal("10000"),
        premium=Decimal("1500"),
        percent_pay=Decimal("750"),
        vacation_pay=Decimal("0"),
        fund_accrual=Decimal("500"),
        deduction=Decimal("250"),
        total_payable=Decimal("12000"),
        deposit_excluded_for_run=False,
        deposit_exclusion_reason=None,
        components=components or {"days": [], "adjustments": {}},
    )


def make_adjustment(
    employee: Employee,
    work_date: date,
    adjustment_type: str,
    amount: Decimal,
    *,
    label: str = "Ручная корректировка",
) -> PayrollAdjustment:
    category = PayrollAdjustmentCategory(
        id=uuid.uuid4(),
        type=adjustment_type,
        code=f"test-{uuid.uuid4()}",
        display_name=label,
        default_amount=amount,
        is_active=True,
        sort_order=0,
    )
    adjustment = PayrollAdjustment(
        id=uuid.uuid4(),
        employee_id=employee.id,
        work_date=work_date,
        type=adjustment_type,
        category_id=category.id,
        amount=amount,
        comment="Комментарий",
    )
    adjustment.category = category
    return adjustment


def make_inventory_audit(
    business_date: date = date(2026, 5, 25),
    *,
    total: Decimal = Decimal("1000.00"),
) -> InventoryAudit:
    return InventoryAudit(
        id=uuid.uuid4(),
        business_date=business_date,
        previous_audit_date=None,
        source="manual",
        status="draft",
        total_shortage_amount=total,
        total_penalty_amount=total,
        computation_snapshot=None,
        notes=None,
    )


def make_deferred_charge_periods(period_count: int = 3) -> list[PayrollPeriod]:
    periods: list[PayrollPeriod] = []
    start = date(2026, 6, 1)
    for index in range(period_count):
        period_start = start + timedelta(days=7 * index)
        periods.append(
            make_period(
                start=period_start,
                end=period_start + timedelta(days=6),
                payroll_date=period_start + timedelta(days=7),
            )
        )
    return periods


async def create_test_deferred_charge(
    session: Any,
    *,
    audit_id: uuid.UUID,
    employee_id: uuid.UUID,
    total_amount: Decimal = Decimal("1000.00"),
    splits_count: int = 3,
) -> DeferredAuditCharge:
    return await create_deferred_charge(
        session,
        DeferredChargeCreate(
            source_audit_id=audit_id,
            employee_id=employee_id,
            total_amount=total_amount,
            splits_count=splits_count,
            reason="Недостача распределяется",
        ),
        actor=CurrentActor(roles=frozenset({"finance_manager"}), user_id=uuid.uuid4()),
    )


class DeferredChargeScalarResult:
    def __init__(self, items: list[Any]) -> None:
        self.items = items

    def all(self) -> list[Any]:
        return self.items


class DeferredChargeFakeSession:
    def __init__(
        self,
        *,
        audit: InventoryAudit | None = None,
        employee: Employee | None = None,
        periods: list[PayrollPeriod] | None = None,
    ) -> None:
        self.audit = audit
        self.employee = employee
        self.periods = {period.id: period for period in periods or []}
        self.charges: list[DeferredAuditCharge] = []
        self.categories: list[PayrollAdjustmentCategory] = []
        self.adjustments: list[PayrollAdjustment] = []
        self.runs: list[PayrollRun] = []
        self.added: list[Any] = []
        self.committed = False

    async def get(self, model: Any, object_id: uuid.UUID) -> Any | None:
        if model is InventoryAudit and self.audit is not None and object_id == self.audit.id:
            return self.audit
        if model is Employee and self.employee is not None and object_id == self.employee.id:
            return self.employee
        if model is PayrollPeriod:
            return self.periods.get(object_id)
        if model is PayrollAdjustment:
            return next(
                (adjustment for adjustment in self.adjustments if adjustment.id == object_id),
                None,
            )
        if model is DeferredAuditCharge:
            return next((charge for charge in self.charges if charge.id == object_id), None)
        return None

    def add(self, item: Any) -> None:
        self.added.append(item)
        if isinstance(item, PayrollAdjustment):
            self.adjustments.append(item)
        elif isinstance(item, PayrollAdjustmentCategory):
            self.categories.append(item)
        elif isinstance(item, DeferredAuditCharge):
            self.charges.append(item)
        elif isinstance(item, PayrollRun):
            self.runs.append(item)

    async def flush(self) -> None:
        for item in [*self.added, *self.charges]:
            if getattr(item, "id", None) is None:
                item.id = uuid.uuid4()
            if isinstance(item, DeferredAuditCharge):
                item.source_audit = self.audit
                item.employee = self.employee
                item.created_by = None
                for split in item.splits:
                    if split.id is None:
                        split.id = uuid.uuid4()
                    split.charge_id = item.id
                    split.charge = item

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _item: Any) -> None:
        return None

    async def execute(self, _stmt: Any) -> Any:
        return PayrollLineExecuteResult([])

    async def scalar(self, query: Any) -> Any:
        entity = query_entity(query)
        if entity is DeferredAuditCharge:
            return self.charges[0] if self.charges else None
        if entity is PayrollAdjustmentCategory:
            return next(
                (category for category in self.categories if category.code == "audit_deferred"),
                None,
            )
        if entity is PayrollRun:
            return None
        return None

    async def scalars(self, query: Any) -> DeferredChargeScalarResult:
        entity = query_entity(query)
        if entity is DeferredAuditCharge:
            return DeferredChargeScalarResult(
                [
                    charge
                    for charge in self.charges
                    if charge.status in {"pending", "partially_applied"}
                ]
            )
        return DeferredChargeScalarResult([])


def app_with_deferred_charge_session(session: DeferredChargeFakeSession):
    app = create_app()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    return app


async def fake_load_attendance_entries(
    _session: Any,
    period: PayrollPeriod,
    *,
    iiko_records: Any = None,
) -> list[AttendanceEntry]:
    employee = _session.employee
    assert employee is not None
    return [make_entry(period, employee, period.start_date)]


async def fake_collect_blocking_issues(
    _session: Any,
    _entries: list[AttendanceEntry],
    *,
    period: PayrollPeriod | None = None,
) -> list[dict[str, Any]]:
    return []


async def fake_ensure_daily_revenue_cached(*_args: Any, **_kwargs: Any) -> dict[date, Decimal]:
    return {}


async def fake_calculate_payroll_lines(
    _session: Any,
    _period: PayrollPeriod,
    _run_id: uuid.UUID,
    _entries: list[AttendanceEntry],
    **_kwargs: Any,
) -> Any:
    return type(
        "FakePayrollCalculation",
        (),
        {"lines": [], "blocking_issues": [], "summary": {}},
    )()


async def fake_update_deposits_and_fund(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {}


async def fake_mark_vacations_paid(*_args: Any, **_kwargs: Any) -> int:
    return 0


def patch_runner_for_deferred_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(payroll_routes, "run_payroll", run_payroll)
    monkeypatch.setattr(
        "app.services.payroll_runner.load_attendance_entries",
        fake_load_attendance_entries,
    )
    monkeypatch.setattr(
        "app.services.payroll_runner.collect_blocking_issues",
        fake_collect_blocking_issues,
    )
    monkeypatch.setattr(
        "app.services.payroll_runner.ensure_daily_revenue_cached",
        fake_ensure_daily_revenue_cached,
    )
    monkeypatch.setattr(
        "app.services.payroll_runner.calculate_payroll_lines",
        fake_calculate_payroll_lines,
    )
    monkeypatch.setattr(
        "app.services.payroll_runner.update_deposits_and_fund",
        fake_update_deposits_and_fund,
    )
    monkeypatch.setattr(
        "app.services.payroll_runner.vacation_service.mark_vacations_paid_for_payroll_period",
        fake_mark_vacations_paid,
    )


async def test_create_deferred_charge_splits_correctly(
) -> None:
    employee = make_employee()
    audit = make_inventory_audit()
    session = DeferredChargeFakeSession(audit=audit, employee=employee)

    with TestClient(app_with_deferred_charge_session(session)) as client:
        response = client.post(
            "/api/v1/payroll/deferred-charges",
            headers={"X-User-Role": "finance_manager"},
            json={
                "source_audit_id": str(audit.id),
                "employee_id": str(employee.id),
                "total_amount": "1000",
                "splits_count": 3,
                "reason": "Распределить штраф",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_amount"] == "1000.00"
    assert payload["splits_count"] == 3
    assert payload["splits_remaining"] == 3
    assert payload["status"] == "pending"
    assert [split["amount"] for split in payload["splits"]] == [
        "333.33",
        "333.33",
        "333.34",
    ]


async def test_apply_pending_splits_creates_adjustments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_runner_for_deferred_tests(monkeypatch)
    employee = make_employee()
    audit = make_inventory_audit()
    periods = make_deferred_charge_periods()
    session = DeferredChargeFakeSession(audit=audit, employee=employee, periods=periods)
    charge = await create_test_deferred_charge(
        session,
        audit_id=audit.id,
        employee_id=employee.id,
    )

    expected_amounts = [Decimal("333.33"), Decimal("333.33"), Decimal("333.34")]
    for index, period in enumerate(periods, start=1):
        run = await run_payroll(session, period.id)  # type: ignore[arg-type]
        split = next(split for split in charge.splits if split.split_index == index)
        assert run.status == "completed"
        assert run.summary["deferred_charges_applied"] == 1
        assert split.run_id == run.id
        assert split.adjustment_id is not None

        adjustment = await session.get(PayrollAdjustment, split.adjustment_id)
        assert adjustment is not None
        assert adjustment.employee_id == employee.id
        assert adjustment.work_date == period.end_date
        assert adjustment.type == "penalty"
        assert adjustment.amount == expected_amounts[index - 1]
        assert adjustment.created_by_label == "system:deferred_charge"

    assert charge.status == "applied"
    assert charge.splits_remaining == 0


async def test_cancel_partially_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_runner_for_deferred_tests(monkeypatch)
    employee = make_employee()
    audit = make_inventory_audit()
    periods = make_deferred_charge_periods(period_count=1)
    session = DeferredChargeFakeSession(audit=audit, employee=employee, periods=periods)
    charge = await create_test_deferred_charge(
        session,
        audit_id=audit.id,
        employee_id=employee.id,
    )
    await run_payroll(session, periods[0].id)  # type: ignore[arg-type]

    cancelled = await cancel_deferred_charge(
        session,
        charge.id,
        actor=CurrentActor(roles=frozenset({"finance_manager"}), user_id=uuid.uuid4()),
    )

    assert cancelled.status == "cancelled"
    assert cancelled.splits_remaining == 0
    splits = sorted(cancelled.splits, key=lambda split: split.split_index)
    assert splits[0].run_id is not None
    assert splits[0].adjustment_id is not None
    for split in splits[1:]:
        assert split.run_id is None
        assert split.adjustment_id is None
        assert split.applied_at is not None


def test_create_validates_audit_employee_existence() -> None:
    session = DeferredChargeFakeSession()
    with TestClient(app_with_deferred_charge_session(session)) as client:
        response = client.post(
            "/api/v1/payroll/deferred-charges",
            headers={"X-User-Role": "finance_manager"},
            json={
                "source_audit_id": str(uuid.uuid4()),
                "employee_id": str(uuid.uuid4()),
                "total_amount": "1000",
                "splits_count": 3,
                "reason": "Распределить штраф",
            },
        )

    assert response.status_code == 404


def test_create_rejects_zero_splits() -> None:
    session = DeferredChargeFakeSession()
    with TestClient(app_with_deferred_charge_session(session)) as client:
        response = client.post(
            "/api/v1/payroll/deferred-charges",
            headers={"X-User-Role": "finance_manager"},
            json={
                "source_audit_id": str(uuid.uuid4()),
                "employee_id": str(uuid.uuid4()),
                "total_amount": "1000",
                "splits_count": 0,
                "reason": "Распределить штраф",
            },
        )

    assert response.status_code == 422


def make_shift_ledger_entry(
    employee_id: uuid.UUID,
    work_date: date,
    *,
    payroll_role: str | None = None,
    category: str | None = None,
    is_resolved: bool = False,
) -> ShiftLedgerEntry:
    return ShiftLedgerEntry(
        id=uuid.uuid4(),
        employee_id=employee_id,
        work_date=work_date,
        payroll_role=payroll_role,
        category=category,
        source="manual_correction" if payroll_role else "fallback_primary",
        opened_at=datetime(work_date.year, work_date.month, work_date.day, 8, 0, tzinfo=UTC),
        closed_at=datetime(work_date.year, work_date.month, work_date.day, 17, 0, tzinfo=UTC),
        is_resolved=is_resolved,
    )


class ShiftLedgerFakeSession:
    def __init__(
        self,
        entry: ShiftLedgerEntry | None = None,
        *,
        role_assignments: list[EmployeeRoleAssignment] | None = None,
    ) -> None:
        self.entry = entry
        self.role_assignments = role_assignments or []
        self.added: list[Any] = []
        self.committed = False

    def add(self, item: Any) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _item: Any) -> None:
        return None

    async def get(self, model: Any, object_id: uuid.UUID) -> Any:
        if model is ShiftLedgerEntry and self.entry is not None and self.entry.id == object_id:
            return self.entry
        return None

    async def scalars(self, _stmt: Any) -> Any:
        return ShiftLedgerScalarResult(self.role_assignments)


class ShiftLedgerMatrixFakeSession:
    def __init__(
        self,
        rows: list[tuple[ShiftLedgerEntry, Employee]],
        *,
        role_assignments: list[EmployeeRoleAssignment] | None = None,
        latest_locked_date: date | None = None,
    ) -> None:
        self.rows = rows
        self.role_assignments = role_assignments or []
        self.latest_locked_date = latest_locked_date

    async def execute(self, _stmt: Any) -> Any:
        return ShiftLedgerExecuteResult(self.rows)

    async def scalars(self, _stmt: Any) -> Any:
        return ShiftLedgerScalarResult(self.role_assignments)

    async def scalar(self, _stmt: Any) -> date | None:
        return self.latest_locked_date


class ShiftLedgerExecuteResult:
    def __init__(self, rows: list[tuple[ShiftLedgerEntry, Employee]]) -> None:
        self.rows = rows

    def all(self) -> list[tuple[ShiftLedgerEntry, Employee]]:
        return self.rows


class ShiftLedgerScalarResult:
    def __init__(self, items: list[Any]) -> None:
        self.items = items

    def all(self) -> list[Any]:
        return self.items


class RevenueCacheFakeSession:
    def __init__(self, setting: AppSetting | None = None) -> None:
        self.setting = setting
        self.added: list[Any] = []

    async def scalar(self, _stmt: Any) -> Any:
        return self.setting

    def add(self, item: Any) -> None:
        self.added.append(item)
        if isinstance(item, AppSetting):
            self.setting = item

    async def flush(self) -> None:
        if self.setting is not None and self.setting.id is None:
            self.setting.id = uuid.uuid4()


class ShiftLedgerAttendanceFakeSession:
    def __init__(self, employees: list[Employee]) -> None:
        self.employees = employees
        self.added: list[Any] = []
        self.flush_count = 0

    async def scalars(self, _stmt: Any) -> Any:
        return ShiftLedgerAttendanceScalarResult(self.employees)

    def add(self, item: Any) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        self.flush_count += 1


class ShiftLedgerAttendanceScalarResult:
    def __init__(self, employees: list[Employee]) -> None:
        self.employees = employees

    def all(self) -> list[Employee]:
        return self.employees


class PayrollLineRouteFakeSession:
    def __init__(self, payout_rows: list[tuple[uuid.UUID, Decimal]]) -> None:
        self.payout_rows = payout_rows
        self.statements: list[Any] = []

    async def execute(self, stmt: Any) -> Any:
        self.statements.append(stmt)
        return PayrollLineExecuteResult(self.payout_rows)


class PayrollLinePatchFakeSession:
    def __init__(
        self,
        line: PayrollLine,
        run: PayrollRun,
        payout_rows: list[tuple[uuid.UUID, Decimal]] | None = None,
    ) -> None:
        self.line = line
        self.run = run
        self.payout_rows = payout_rows or []
        self.added: list[Any] = []
        self.committed = False

    async def get(self, model: Any, object_id: uuid.UUID) -> Any | None:
        if model is PayrollLine and object_id == self.line.id:
            return self.line
        if model is PayrollRun and object_id == self.run.id:
            return self.run
        return None

    async def execute(self, stmt: Any) -> Any:
        return PayrollLineExecuteResult(self.payout_rows)

    def add(self, item: Any) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _item: Any) -> None:
        return None


class PayrollLineExecuteResult:
    def __init__(self, rows: list[tuple[uuid.UUID, Decimal]]) -> None:
        self.rows = rows

    def all(self) -> list[tuple[uuid.UUID, Decimal]]:
        return self.rows


class PersonalReportExecuteResult:
    def __init__(self, rows: list[tuple[PayrollLine, PayrollRun, PayrollPeriod]]) -> None:
        self.rows = rows

    def all(self) -> list[tuple[PayrollLine, PayrollRun, PayrollPeriod]]:
        return self.rows


class PersonalReportScalarResult:
    def __init__(self, items: list[Any]) -> None:
        self.items = items

    def all(self) -> list[Any]:
        return self.items


class PersonalReportFakeSession:
    def __init__(
        self,
        *,
        employees: list[Employee] | None = None,
        line_rows: list[tuple[PayrollLine, PayrollRun, PayrollPeriod]] | None = None,
        adjustments: list[PayrollAdjustment] | None = None,
        deposit_transactions: list[DepositTransaction] | None = None,
    ) -> None:
        self.employees = {employee.id: employee for employee in employees or []}
        self.line_rows = line_rows or []
        self.adjustments = adjustments or []
        self.deposit_transactions = deposit_transactions or []

    async def get(self, model: Any, object_id: uuid.UUID) -> Any | None:
        if model is Employee:
            return self.employees.get(object_id)
        return None

    async def execute(self, _stmt: Any) -> PersonalReportExecuteResult:
        return PersonalReportExecuteResult(self.line_rows)

    async def scalars(self, query: Any) -> PersonalReportScalarResult:
        entity = query_entity(query)
        if entity is PayrollAdjustment:
            return PersonalReportScalarResult(self.adjustments)
        if entity is DepositTransaction:
            return PersonalReportScalarResult(self.deposit_transactions)
        return PersonalReportScalarResult([])


class FundScalarResult:
    def __init__(self, items: list[Any]) -> None:
        self.items = items

    def all(self) -> list[Any]:
        return self.items


class FundFakeSession:
    def __init__(
        self,
        *,
        employees: list[Employee] | None = None,
        accounts: list[AccumulationFundAccount] | None = None,
        transactions: list[AccumulationFundTransaction] | None = None,
    ) -> None:
        self.employees = {employee.id: employee for employee in employees or []}
        self.accounts = {account.id: account for account in accounts or []}
        self.transactions = transactions or []
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.committed = False

    async def get(self, model: Any, object_id: uuid.UUID) -> Any | None:
        if model is Employee:
            return self.employees.get(object_id)
        if model is AccumulationFundAccount:
            return self.accounts.get(object_id)
        return None

    async def scalar(self, query: Any) -> Any | None:
        entity = query_entity(query)
        if entity is AccumulationFundAccount:
            return next(iter(self.accounts.values()), None)
        if entity is Employee:
            return next(iter(self.employees.values()), None)
        return None

    async def scalars(self, query: Any) -> FundScalarResult:
        entity = query_entity(query)
        sql = str(query.compile(compile_kwargs={"literal_binds": True}))
        if entity is AccumulationFundTransaction:
            transactions = list(self.transactions)
            if "transaction_type = 'payout'" in sql:
                transactions = [
                    transaction
                    for transaction in transactions
                    if transaction.transaction_type == "payout"
                ]
            if "transaction_type = 'forfeit'" in sql:
                transactions = [
                    transaction
                    for transaction in transactions
                    if transaction.transaction_type == "forfeit"
                ]
            if "transaction_type = 'accrual'" in sql:
                transactions = [
                    transaction
                    for transaction in transactions
                    if transaction.transaction_type == "accrual"
                ]
            return FundScalarResult(transactions)
        if entity is AccumulationFundAccount:
            accounts = list(self.accounts.values())
            if "status = 'active'" in sql:
                accounts = [account for account in accounts if account.status == "active"]
            return FundScalarResult(accounts)
        if entity is Employee:
            return FundScalarResult(list(self.employees.values()))
        return FundScalarResult([])

    def add(self, item: Any) -> None:
        self.added.append(item)
        if isinstance(item, AccumulationFundAccount):
            if item.id is None:
                item.id = uuid.uuid4()
            self.accounts[item.id] = item
        if isinstance(item, AccumulationFundTransaction):
            if item.id is None:
                item.id = uuid.uuid4()
            self.transactions.append(item)

    async def delete(self, item: Any) -> None:
        self.deleted.append(item)
        if isinstance(item, AccumulationFundTransaction) and item in self.transactions:
            self.transactions.remove(item)

    async def flush(self) -> None:
        for item in self.added:
            if getattr(item, "id", None) is None:
                item.id = uuid.uuid4()

    async def commit(self) -> None:
        self.committed = True


async def _empty_assignments(*_args, **_kwargs) -> dict:
    return {}


async def _prepare_shift_ledger_build(
    monkeypatch: pytest.MonkeyPatch,
    employee_id: uuid.UUID,
    *,
    schedule: dict[uuid.UUID, LedgerAssignment],
    primary: dict[uuid.UUID, Any],
) -> None:
    async def fake_snapshots(*_args, **_kwargs):
        return [
            AttendanceSnapshot(
                employee_id=employee_id,
                opened_at=datetime(2026, 5, 28, 8, 0, tzinfo=UTC),
                closed_at=datetime(2026, 5, 28, 17, 0, tzinfo=UTC),
            )
        ]

    async def fake_schedule(*_args, **_kwargs):
        return schedule

    async def fake_primary(*_args, **_kwargs):
        return primary

    monkeypatch.setattr(shift_ledger_service, "load_iiko_attendance_snapshots", fake_snapshots)
    monkeypatch.setattr(shift_ledger_service, "load_schedule_assignments", fake_schedule)
    monkeypatch.setattr(shift_ledger_service, "load_available_role_assignments", fake_primary)
    monkeypatch.setattr(shift_ledger_service, "load_existing_entries", _empty_assignments)


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


def test_closed_shift_over_12_hours_is_capped_without_quality_review() -> None:
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

    assert entry.minutes_worked == 12 * 60
    assert entry.quality_status == "ok"
    assert "capped_to_12h_from_780min" in (entry.notes or "")


async def test_load_shift_ledger_iiko_snapshots_skips_unknown_iiko_employee() -> None:
    employee = make_employee()
    work_date = date(2026, 5, 28)
    session = ShiftLedgerAttendanceFakeSession([employee])

    snapshots = await shift_ledger_service.load_iiko_attendance_snapshots(
        session,  # type: ignore[arg-type]
        work_date,
        iiko_records=[
            {
                "employeeId": employee.iiko_id,
                "dateFrom": "2026-05-28T08:00:00+03:00",
                "dateTo": "2026-05-28T17:00:00+03:00",
            },
            {
                "employeeId": "unknown-courier-iiko-id",
                "employeeName": "Unknown Courier",
                "dateFrom": "2026-05-28T09:00:00+03:00",
                "dateTo": "2026-05-28T12:00:00+03:00",
            },
        ],
    )

    assert len(snapshots) == 1
    assert snapshots[0].employee_id == employee.id
    assert session.added == []
    assert session.flush_count == 0


async def test_build_shift_ledger_creates_entries_from_iiko_attendance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    employee = make_employee()
    work_date = date(2026, 5, 28)

    async def fake_snapshots(*_args, **_kwargs):
        return [
            AttendanceSnapshot(
                employee_id=employee.id,
                opened_at=datetime(2026, 5, 28, 8, 0, tzinfo=UTC),
                closed_at=None,
            )
        ]

    async def fake_schedule(*_args, **_kwargs):
        return {employee.id: LedgerAssignment("pizza", "category_2")}

    async def fake_roles(*_args, **_kwargs):
        return {employee.id: [LedgerAssignment("pizza", "category_2")]}

    monkeypatch.setattr(shift_ledger_service, "load_iiko_attendance_snapshots", fake_snapshots)
    monkeypatch.setattr(shift_ledger_service, "load_schedule_assignments", fake_schedule)
    monkeypatch.setattr(shift_ledger_service, "load_available_role_assignments", fake_roles)
    monkeypatch.setattr(shift_ledger_service, "load_existing_entries", _empty_assignments)

    session = ShiftLedgerFakeSession()
    entries = await shift_ledger_service.build_ledger_for_date(session, work_date)  # type: ignore[arg-type]

    assert len(entries) == 1
    assert isinstance(session.added[0], ShiftLedgerEntry)
    assert entries[0].employee_id == employee.id
    assert entries[0].opened_at == datetime(2026, 5, 28, 8, 0, tzinfo=UTC)
    assert session.committed is True


async def test_build_shift_ledger_prefers_schedule_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    employee = make_employee()
    work_date = date(2026, 5, 28)

    await _prepare_shift_ledger_build(
        monkeypatch,
        employee.id,
        schedule={employee.id: LedgerAssignment("pizza", "category_2")},
        primary={
            employee.id: [
                LedgerAssignment("sushi", "category_1", is_primary=True),
                LedgerAssignment("pizza", "category_2"),
            ]
        },
    )

    entries = await shift_ledger_service.build_ledger_for_date(  # type: ignore[arg-type]
        ShiftLedgerFakeSession(),
        work_date,
    )

    assert entries[0].payroll_role == "pizza"
    assert entries[0].category == "category_2"
    assert entries[0].source == "schedule"
    assert entries[0].is_resolved is True


async def test_build_shift_ledger_falls_back_to_primary_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    employee = make_employee()
    work_date = date(2026, 5, 28)

    await _prepare_shift_ledger_build(
        monkeypatch,
        employee.id,
        schedule={},
        primary={
            employee.id: [
                LedgerAssignment("sushi", "category_1", is_primary=True),
                LedgerAssignment("pizza", "category_2"),
            ]
        },
    )

    entries = await shift_ledger_service.build_ledger_for_date(  # type: ignore[arg-type]
        ShiftLedgerFakeSession(),
        work_date,
    )

    assert entries[0].payroll_role == "sushi"
    assert entries[0].category == "category_1"
    assert entries[0].source == "fallback_primary"
    assert entries[0].is_resolved is True


async def test_build_shift_ledger_single_assignment_resolves_without_dropdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    employee = make_employee()
    work_date = date(2026, 5, 28)

    await _prepare_shift_ledger_build(
        monkeypatch,
        employee.id,
        schedule={},
        primary={employee.id: [LedgerAssignment("sushi", "category_1")]},
    )

    entries = await shift_ledger_service.build_ledger_for_date(  # type: ignore[arg-type]
        ShiftLedgerFakeSession(),
        work_date,
    )

    assert entries[0].payroll_role == "sushi"
    assert entries[0].category == "category_1"
    assert entries[0].is_resolved is True


async def test_build_shift_ledger_marks_missing_assignment_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    employee = make_employee()
    work_date = date(2026, 5, 28)

    await _prepare_shift_ledger_build(monkeypatch, employee.id, schedule={}, primary={})

    entries = await shift_ledger_service.build_ledger_for_date(  # type: ignore[arg-type]
        ShiftLedgerFakeSession(),
        work_date,
    )

    assert entries[0].payroll_role is None
    assert entries[0].category is None
    assert entries[0].source == "fallback_primary"
    assert entries[0].is_resolved is False


async def test_build_shift_ledger_multiple_assignments_without_primary_needs_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    employee = make_employee()
    work_date = date(2026, 5, 28)

    await _prepare_shift_ledger_build(
        monkeypatch,
        employee.id,
        schedule={},
        primary={
            employee.id: [
                LedgerAssignment("sushi", "category_1"),
                LedgerAssignment("pizza", "category_2"),
            ]
        },
    )

    entries = await shift_ledger_service.build_ledger_for_date(  # type: ignore[arg-type]
        ShiftLedgerFakeSession(),
        work_date,
    )

    assert entries[0].payroll_role is None
    assert entries[0].category is None
    assert entries[0].is_resolved is False


async def test_patch_shift_ledger_sets_manual_correction_and_audit() -> None:
    employee_id = uuid.uuid4()
    entry = ShiftLedgerEntry(
        id=uuid.uuid4(),
        employee_id=employee_id,
        work_date=date(2026, 5, 28),
        payroll_role=None,
        category=None,
        source="fallback_primary",
        opened_at=datetime(2026, 5, 28, 8, 0, tzinfo=UTC),
        closed_at=None,
        is_resolved=False,
    )

    session = ShiftLedgerFakeSession(
        entry,
        role_assignments=[make_role_assignment(employee_id, "pizza", "category_2")],
    )

    corrected = await shift_ledger_service.manually_correct(
        session,  # type: ignore[arg-type]
        entry.id,
        "pizza",
    )

    actions = [item for item in session.added if isinstance(item, AgentAction)]
    assert corrected.payroll_role == "pizza"
    assert corrected.category == "category_2"
    assert corrected.source == "manual_correction"
    assert corrected.is_resolved is True
    assert actions[0].target_table == "shift_ledger_entry"
    assert session.committed is True


async def test_patch_shift_ledger_rejects_role_not_in_staff_assignments() -> None:
    employee_id = uuid.uuid4()
    entry = ShiftLedgerEntry(
        id=uuid.uuid4(),
        employee_id=employee_id,
        work_date=date(2026, 5, 28),
        payroll_role=None,
        category=None,
        source="fallback_primary",
        opened_at=datetime(2026, 5, 28, 8, 0, tzinfo=UTC),
        closed_at=None,
        is_resolved=False,
    )

    with pytest.raises(shift_ledger_service.ShiftLedgerValidationError, match="Штате"):
        await shift_ledger_service.manually_correct(  # type: ignore[arg-type]
            ShiftLedgerFakeSession(
                entry,
                role_assignments=[make_role_assignment(employee_id, "sushi", "category_1")],
            ),
            entry.id,
            "pizza",
        )


def test_patch_shift_ledger_route_returns_400_for_unassigned_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app()
    client = TestClient(app)
    employee_id = uuid.uuid4()
    entry = ShiftLedgerEntry(
        id=uuid.uuid4(),
        employee_id=employee_id,
        work_date=date(2026, 5, 28),
        payroll_role=None,
        category=None,
        source="fallback_primary",
        opened_at=datetime(2026, 5, 28, 8, 0, tzinfo=UTC),
        closed_at=None,
        is_resolved=False,
    )
    session = ShiftLedgerFakeSession(entry)

    async def override_session():
        yield session

    async def fake_latest_locked_date(_session):
        return None

    async def fake_manually_correct(*_args, **_kwargs):
        raise shift_ledger_service.ShiftLedgerValidationError(
            "Эта роль не закреплена за сотрудником в Штате"
        )

    app.dependency_overrides[get_session] = override_session
    monkeypatch.setattr(shift_routes, "get_latest_locked_payroll_date", fake_latest_locked_date)
    monkeypatch.setattr(shift_routes, "manually_correct", fake_manually_correct)

    response = client.patch(
        f"/api/v1/shifts/ledger/{entry.id}",
        headers={"X-User-Role": "manager"},
        json={"payroll_role": "pizza"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Эта роль не закреплена за сотрудником в Штате"
    app.dependency_overrides.clear()
    client.close()


async def test_ledger_matrix_without_payroll_run_uses_current_roles_and_unlocked() -> None:
    employee = make_employee()
    entry = make_shift_ledger_entry(employee.id, date(2026, 5, 24))
    role = make_role_assignment(
        employee.id,
        "pizza",
        "category_2",
        effective_from=date(2026, 5, 30),
    )
    session = ShiftLedgerMatrixFakeSession(
        [(entry, employee)],
        role_assignments=[role],
        latest_locked_date=None,
    )

    matrix = await shift_ledger_service.list_ledger_matrix(  # type: ignore[arg-type]
        session,
        date(2026, 5, 30),
    )
    employee_days = matrix["employees"][0]["days"]

    assert all(day["payroll_locked"] is False for day in employee_days)
    assert all(shift["payroll_locked"] is False for day in employee_days for shift in day["shifts"])
    assert employee_days[0]["date"] == "2026-05-24"
    assert employee_days[0]["available_roles"] == [
        {"payroll_role": "pizza", "category": "category_2", "is_substitute": False}
    ]


async def test_ledger_matrix_marks_days_locked_through_latest_completed_run() -> None:
    employee = make_employee()
    rows = [
        (make_shift_ledger_entry(employee.id, work_date), employee)
        for work_date in shift_ledger_service.iter_dates(date(2026, 5, 24), date(2026, 5, 30))
    ]
    session = ShiftLedgerMatrixFakeSession(
        rows,
        role_assignments=[make_role_assignment(employee.id, "pizza", "category_2")],
        latest_locked_date=date(2026, 5, 24),
    )

    matrix = await shift_ledger_service.list_ledger_matrix(  # type: ignore[arg-type]
        session,
        date(2026, 5, 30),
    )
    locked_by_date = {
        day["date"]: (day["payroll_locked"], day["shifts"][0]["payroll_locked"])
        for day in matrix["employees"][0]["days"]
    }

    assert locked_by_date["2026-05-24"] == (True, True)
    assert all(
        locked_by_date[work_date.isoformat()] == (False, False)
        for work_date in shift_ledger_service.iter_dates(date(2026, 5, 25), date(2026, 5, 30))
    )


def test_patch_shift_ledger_route_rejects_locked_period_without_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app()
    client = TestClient(app)
    employee_id = uuid.uuid4()
    entry = make_shift_ledger_entry(employee_id, date(2026, 5, 24))
    session = ShiftLedgerFakeSession(
        entry,
        role_assignments=[make_role_assignment(employee_id, "pizza", "category_2")],
    )

    async def override_session():
        yield session

    async def fake_latest_locked_date(_session):
        return date(2026, 5, 24)

    app.dependency_overrides[get_session] = override_session
    monkeypatch.setattr(shift_routes, "get_latest_locked_payroll_date", fake_latest_locked_date)

    response = client.patch(
        f"/api/v1/shifts/ledger/{entry.id}",
        headers={"X-User-Role": "manager"},
        json={"payroll_role": "pizza"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "ЗП за эту неделю уже закрыта, изменение роли невозможно"
    assert [item for item in session.added if isinstance(item, AgentAction)] == []
    app.dependency_overrides.clear()
    client.close()


def test_patch_shift_ledger_route_allows_unlocked_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app()
    client = TestClient(app)
    employee_id = uuid.uuid4()
    entry = make_shift_ledger_entry(employee_id, date(2026, 5, 25))
    session = ShiftLedgerFakeSession(
        entry,
        role_assignments=[make_role_assignment(employee_id, "pizza", "category_2")],
    )

    async def override_session():
        yield session

    async def fake_latest_locked_date(_session):
        return date(2026, 5, 24)

    async def fake_list_ledger_for_date(_session, _work_date):
        return [
            {
                "id": str(entry.id),
                "work_date": entry.work_date.isoformat(),
                "employee_id": str(entry.employee_id),
                "employee_name": "Payroll Employee",
                "employee_iiko_id": "iiko-test",
                "payroll_role": entry.payroll_role,
                "category": entry.category,
                "source": entry.source,
                "opened_at": entry.opened_at.isoformat(),
                "closed_at": entry.closed_at.isoformat() if entry.closed_at else None,
                "notes": entry.notes,
                "is_resolved": entry.is_resolved,
                "status": "resolved",
                "available_roles": [
                    {"payroll_role": "pizza", "category": "category_2", "is_substitute": False}
                ],
            }
        ]

    app.dependency_overrides[get_session] = override_session
    monkeypatch.setattr(shift_routes, "get_latest_locked_payroll_date", fake_latest_locked_date)
    monkeypatch.setattr(shift_routes, "list_ledger_for_date", fake_list_ledger_for_date)

    response = client.patch(
        f"/api/v1/shifts/ledger/{entry.id}",
        headers={"X-User-Role": "manager"},
        json={"payroll_role": "pizza"},
    )

    assert response.status_code == 200
    assert response.json()["payroll_role"] == "pizza"
    assert [item for item in session.added if isinstance(item, AgentAction)]
    app.dependency_overrides.clear()
    client.close()


def test_ledger_response_category_is_computed_from_staff_assignment() -> None:
    employee = make_employee()
    entry = ShiftLedgerEntry(
        id=uuid.uuid4(),
        employee_id=employee.id,
        work_date=date(2026, 5, 28),
        payroll_role="pizza",
        category="category_1",
        source="manual_correction",
        opened_at=datetime(2026, 5, 28, 8, 0, tzinfo=UTC),
        closed_at=None,
        is_resolved=True,
    )

    row = shift_ledger_service.serialize_ledger_entry(
        entry,
        employee,
        [LedgerAssignment("pizza", "category_2")],
    )

    assert row["category"] == "category_2"
    assert row["status"] == "resolved"
    assert row["available_roles"] == [
        {"payroll_role": "pizza", "category": "category_2", "is_substitute": False}
    ]


def test_ledger_response_serializes_substitute_available_role() -> None:
    employee = make_employee(position="Управляющий", category=None, default_cooking_station=None)
    entry = make_shift_ledger_entry(employee.id, date(2026, 5, 28))

    row = shift_ledger_service.serialize_ledger_entry(
        entry,
        employee,
        [LedgerAssignment("sushi", "category_1", is_substitute=True)],
    )

    assert row["available_roles"] == [
        {"payroll_role": "sushi", "category": "category_1", "is_substitute": True}
    ]


def test_ledger_response_marks_employee_without_assignments_as_needs_setup() -> None:
    employee = make_employee()
    entry = ShiftLedgerEntry(
        id=uuid.uuid4(),
        employee_id=employee.id,
        work_date=date(2026, 5, 28),
        payroll_role=None,
        category=None,
        source="fallback_primary",
        opened_at=datetime(2026, 5, 28, 8, 0, tzinfo=UTC),
        closed_at=None,
        is_resolved=False,
    )

    row = shift_ledger_service.serialize_ledger_entry(entry, employee, [])

    assert row["category"] is None
    assert row["status"] == "needs_employee_setup"
    assert row["available_roles"] == []


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
        Decimal("6750"),
        [
            PercentShift("employee-1", "category_1", Decimal("12")),
            PercentShift("employee-2", "category_2", Decimal("12")),
            PercentShift("employee-3", "category_3", Decimal("12")),
        ],
    )

    assert distribution == {
        "employee-1": Decimal("3000"),
        "employee-2": Decimal("2250"),
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
    employee = make_employee(position="Повар", category="category_2")
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
    assert result.lines[0].base_pay == 15800
    assert result.lines[0].premium == 0
    assert result.lines[0].total_payable == 15800


def test_vacation_day_creates_payroll_line_without_attendance() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee()
    work_date = date(2026, 5, 20)
    settings = payroll_settings()
    settings[VACATION_DAYS_CONFIG_KEY] = {(employee.id, work_date): None}
    settings[VACATION_DAILY_AMOUNT_CONFIG_KEY] = Decimal("1000")
    settings[EMPLOYEE_ASSIGNMENTS_CONFIG_KEY] = {
        (employee.id, work_date): [
            make_role_assignment(employee.id, "pizza", "category_2", is_primary=True)
        ]
    }

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [],
        {employee.id: employee},
        settings,
    )

    assert result.blocking_issues == []
    assert result.lines[0].base_pay == 0
    assert result.lines[0].percent_pay == 0
    assert result.lines[0].vacation_pay == 1000
    assert result.lines[0].fund_accrual == 0
    assert result.lines[0].total_payable == 1000
    assert result.lines[0].components["days"][0]["kind"] == "vacation"


def test_vacation_day_suppresses_regular_pay_for_attendance_entry() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee()
    work_date = date(2026, 5, 20)
    settings = payroll_settings({work_date.isoformat(): 140000})
    settings[VACATION_DAYS_CONFIG_KEY] = {(employee.id, work_date): None}
    settings[VACATION_DAILY_AMOUNT_CONFIG_KEY] = Decimal("1000")
    settings[EMPLOYEE_ASSIGNMENTS_CONFIG_KEY] = {
        (employee.id, work_date): [
            make_role_assignment(employee.id, "pizza", "category_2", is_primary=True)
        ]
    }

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [make_entry(period, employee, work_date, role=None)],
        {employee.id: employee},
        settings,
    )

    assert result.blocking_issues == []
    assert result.lines[0].base_pay == 0
    assert result.lines[0].percent_pay == 0
    assert result.lines[0].vacation_pay == 1000
    assert result.lines[0].total_payable == 1000
    assert result.lines[0].components["days"][0]["hours"] == 0


def test_full_12_hour_shift_gets_full_salary_and_percent() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee()
    work_date = date(2026, 5, 20)
    entry = make_entry(period, employee, work_date, minutes=720)

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [entry],
        {employee.id: employee},
        payroll_settings({work_date.isoformat(): 140000}),
    )

    assert result.blocking_issues == []
    assert result.lines[0].base_pay == 2200
    assert result.lines[0].percent_pay == 6300


def test_11_hour_shift_prorates_salary_but_keeps_full_single_employee_percent_pool() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee()
    work_date = date(2026, 5, 20)
    entry = make_entry(period, employee, work_date, minutes=660)

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [entry],
        {employee.id: employee},
        payroll_settings({work_date.isoformat(): 140000}),
    )

    assert result.blocking_issues == []
    assert Decimal(str(result.lines[0].base_pay)) == Decimal("2016.67")
    assert result.lines[0].percent_pay == 6300


def test_short_shift_percent_pool_is_not_prorated_twice() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    full_shift_employee = make_employee()
    short_shift_employee = make_employee()
    work_date = date(2026, 5, 20)
    entries = [
        make_entry(period, full_shift_employee, work_date, minutes=720),
        make_entry(period, short_shift_employee, work_date, minutes=360),
    ]

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        entries,
        {
            full_shift_employee.id: full_shift_employee,
            short_shift_employee.id: short_shift_employee,
        },
        payroll_settings({work_date.isoformat(): 140000}),
    )

    assert result.blocking_issues == []
    percent_by_employee = {line.employee_id: line.percent_pay for line in result.lines}
    assert percent_by_employee[full_shift_employee.id] == 4200
    assert percent_by_employee[short_shift_employee.id] == 2100
    assert sum(percent_by_employee.values()) == 6300


def test_weekday_premium_now_in_base_pay() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee()
    entry = make_entry(period, employee, date(2026, 5, 22), minutes=480)

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [entry],
        {employee.id: employee},
        payroll_settings(),
    )

    assert result.blocking_issues == []
    assert result.lines[0].premium == 0
    assert Decimal(str(result.lines[0].base_pay)) == Decimal("1666.67")
    assert Decimal(str(result.lines[0].total_payable)) == Decimal("1666.67")
    day = result.lines[0].components["days"][0]
    assert day["weekday_premium"] == 200
    assert Decimal(str(day["base_pay_shift"])) == Decimal("1466.67")
    assert "premium" not in day


def test_weekday_premium_does_not_apply_below_8_hour_threshold() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee()
    entry = make_entry(period, employee, date(2026, 5, 22), minutes=479)

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [entry],
        {employee.id: employee},
        payroll_settings(),
    )

    assert result.blocking_issues == []
    assert Decimal(str(result.lines[0].base_pay)) == Decimal("1463.61")
    assert result.lines[0].premium == 0
    assert result.lines[0].components["days"][0]["weekday_premium"] == 0


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
    assert result.lines[0].base_pay == 2400
    assert result.lines[0].premium == 0
    assert result.lines[0].total_payable == 2400


def test_weekday_premium_applies_once_for_multiple_saturday_shifts_at_threshold() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee()
    work_date = date(2026, 5, 23)
    entries = [
        make_entry(period, employee, work_date, minutes=300, station="oven"),
        make_entry(period, employee, work_date, minutes=300, station="prep"),
    ]

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        entries,
        {employee.id: employee},
        payroll_settings(),
    )

    assert result.blocking_issues == []
    assert result.lines[0].premium == 0
    assert sum(day["weekday_premium"] for day in result.lines[0].components["days"]) == 200


def test_weekday_premium_does_not_apply_for_multiple_saturday_shifts_below_threshold() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee()
    work_date = date(2026, 5, 23)
    entries = [
        make_entry(period, employee, work_date, minutes=240, station="oven"),
        make_entry(period, employee, work_date, minutes=200, station="prep"),
    ]

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        entries,
        {employee.id: employee},
        payroll_settings(),
    )

    assert result.blocking_issues == []
    assert result.lines[0].premium == 0
    assert sum(day["weekday_premium"] for day in result.lines[0].components["days"]) == 0


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


def test_weekday_premium_uses_updated_amount_setting_on_next_run() -> None:
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
    settings["payroll.weekday_premium"] = {"amount": 250, "threshold_hours": 8}
    second_result = calculate_payroll_lines_from_inputs(
        period,
        uuid.uuid4(),
        [entry],
        {employee.id: employee},
        settings,
    )

    assert first_result.lines[0].premium == 0
    assert first_result.lines[0].base_pay == 2400
    assert second_result.lines[0].premium == 0
    assert second_result.lines[0].base_pay == 2450
    assert second_result.lines[0].total_payable == 2450


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
    assert result.lines[0].base_pay == 2200
    assert result.lines[0].premium == 0
    assert result.lines[0].percent_pay == 0
    assert result.lines[0].total_payable == 2200


def test_manual_bonus_increases_premium() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee()
    work_date = date(2026, 5, 20)
    entry = make_entry(period, employee, work_date)
    bonus = make_adjustment(
        employee,
        work_date,
        "bonus",
        Decimal("1000"),
        label="Качественная работа",
    )
    settings = payroll_settings()
    settings[PAYROLL_ADJUSTMENTS_CONFIG_KEY] = {(employee.id, work_date): [bonus]}

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [entry],
        {employee.id: employee},
        settings,
    )

    assert result.blocking_issues == []
    assert result.lines[0].premium == 1000
    assert result.lines[0].total_payable == 3200
    assert (
        result.lines[0].components["adjustments"]["bonuses"][0]["category"]
        == "Качественная работа"
    )


def test_manual_penalty_increases_deduction() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee()
    work_date = date(2026, 5, 20)
    entry = make_entry(period, employee, work_date)
    penalty = make_adjustment(employee, work_date, "penalty", Decimal("500"), label="Опоздание")
    settings = payroll_settings()
    settings[PAYROLL_ADJUSTMENTS_CONFIG_KEY] = {(employee.id, work_date): [penalty]}

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [entry],
        {employee.id: employee},
        settings,
    )

    assert result.blocking_issues == []
    assert result.lines[0].deduction == 500
    assert result.lines[0].total_payable == 1700
    assert result.lines[0].components["adjustments"]["penalties"][0]["amount"] == "500.00"


def test_adjustment_for_employee_with_two_roles_goes_to_primary() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee()
    work_date = date(2026, 5, 20)
    entries = [
        make_entry(period, employee, work_date, minutes=360, role="Сушист"),
        make_entry(period, employee, work_date, minutes=360, role="Пиццерист"),
    ]
    bonus = make_adjustment(employee, work_date, "bonus", Decimal("1000"))
    settings = payroll_settings()
    settings[EMPLOYEE_ASSIGNMENTS_CONFIG_KEY] = {
        (employee.id, work_date): [
            make_role_assignment(employee.id, "sushi", "category_2"),
            make_role_assignment(employee.id, "pizza", "category_2", is_primary=True),
        ]
    }
    settings[PAYROLL_ADJUSTMENTS_CONFIG_KEY] = {(employee.id, work_date): [bonus]}

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        entries,
        {employee.id: employee},
        settings,
    )

    lines_by_role = {line.role: line for line in result.lines}
    assert lines_by_role["Пиццерист"].premium == 1000
    assert lines_by_role["Сушист"].premium == 0
    assert (
        lines_by_role["Пиццерист"].components["adjustments"]["primary_role_chosen"]
        == "Пиццерист"
    )


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


def test_payroll_calculator_uses_assignment_category_by_work_date() -> None:
    period = make_period(
        start=date(2026, 6, 1),
        end=date(2026, 6, 7),
        payroll_date=date(2026, 6, 8),
    )
    run_id = uuid.uuid4()
    employee = make_employee(
        position="Повар",
        category="category_3",
        default_cooking_station="pizza",
    )
    before_change = date(2026, 6, 6)
    after_change = date(2026, 6, 7)
    entries = [
        make_entry(period, employee, before_change, role="Пиццерист", station="pizza"),
        make_entry(period, employee, after_change, role="Пиццерист", station="pizza"),
    ]
    settings = payroll_settings()
    settings["payroll.role_category_rates"] = {
        "Пиццерист": {"category_2": 2200, "category_3": 1800}
    }
    settings[EMPLOYEE_ASSIGNMENTS_CONFIG_KEY] = {
        (employee.id, before_change): [
            EmployeeRoleAssignment(
                id=uuid.uuid4(),
                employee_id=employee.id,
                payroll_role="pizza",
                category="category_3",
                is_primary=True,
                effective_from=date(2026, 1, 1),
                effective_to=after_change,
            )
        ],
        (employee.id, after_change): [
            EmployeeRoleAssignment(
                id=uuid.uuid4(),
                employee_id=employee.id,
                payroll_role="pizza",
                category="category_2",
                is_primary=True,
                effective_from=after_change,
            )
        ],
    }

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        entries,
        {employee.id: employee},
        settings,
    )

    assert result.blocking_issues == []
    assert result.lines[0].base_pay == 4200
    assert [day["category"] for day in result.lines[0].components["days"]] == [
        "category_3",
        "category_2",
    ]
    assert result.lines[0].components["days"][0]["weekday_premium"] == 200


def test_payroll_calculator_uses_allowance_events_by_work_date() -> None:
    period = make_period(
        start=date(2026, 6, 1),
        end=date(2026, 6, 7),
        payroll_date=date(2026, 6, 8),
    )
    run_id = uuid.uuid4()
    employee = make_employee(position="Повар", category="category_2")
    employee.is_senior = False
    first_day = date(2026, 6, 1)
    second_day = date(2026, 6, 2)
    entries = [
        make_entry(period, employee, first_day),
        make_entry(period, employee, second_day),
    ]
    settings = payroll_settings()
    settings[EMPLOYEE_ALLOWANCES_CONFIG_KEY] = {
        (employee.id, first_day): {"is_senior": False, "is_deputy_senior": False},
        (employee.id, second_day): {"is_senior": True, "is_deputy_senior": False},
    }
    settings[SENIORITY_ALLOWANCE_MAP_CONFIG_KEY] = {
        second_day: {("Повар", "senior"): Decimal("500")}
    }

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        entries,
        {employee.id: employee},
        settings,
    )

    assert result.blocking_issues == []
    assert result.lines[0].base_pay == 4900
    assert [day["base_pay"] for day in result.lines[0].components["days"]] == [2200, 2700]


def test_payroll_calculator_uses_shift_ledger_role_and_category() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee(category="category_1")
    entry = make_entry(period, employee, period.start_date, role="Сушист")
    settings = payroll_settings()
    settings[SHIFT_LEDGER_CONFIG_KEY] = {
        (employee.id, period.start_date): ShiftLedgerEntry(
            id=uuid.uuid4(),
            employee_id=employee.id,
            work_date=period.start_date,
            payroll_role="pizza",
            category="category_2",
            source="manual_correction",
            opened_at=datetime(2026, 5, 19, 8, 0, tzinfo=UTC),
            closed_at=datetime(2026, 5, 19, 17, 0, tzinfo=UTC),
            is_resolved=True,
        )
    }

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [entry],
        {employee.id: employee},
        settings,
    )

    assert result.blocking_issues == []
    assert result.lines[0].role == "pizza"
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


def test_percent_from_revenue_uses_iiko_daily_revenue_before_mock() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    pizza_employee = make_employee()
    sushi_employee = make_employee()
    work_date = period.start_date
    entries = [
        make_entry(period, pizza_employee, work_date, role="Пиццерист"),
        make_entry(period, sushi_employee, work_date, role="Сушист"),
    ]
    settings = payroll_settings({work_date.isoformat(): 50000})
    settings[DAILY_REVENUE_CONFIG_KEY] = {work_date.isoformat(): "350000"}

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        entries,
        {pizza_employee.id: pizza_employee, sushi_employee.id: sushi_employee},
        settings,
    )

    percent_by_employee = {line.employee_id: line.percent_pay for line in result.lines}

    assert result.blocking_issues == []
    assert percent_by_employee[pizza_employee.id] == 9625
    assert percent_by_employee[sushi_employee.id] == 9625
    assert result.lines[0].components["days"][0]["daily_percent_pool"] == 19250


async def test_daily_revenue_cache_writes_and_reuses_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[date, date]] = []

    async def fake_fetch_daily_revenue(
        _session: Any,
        date_from: date,
        date_to: date,
    ) -> dict[date, Decimal]:
        calls.append((date_from, date_to))
        return {date_from: Decimal("350000")}

    monkeypatch.setattr(
        "app.services.payroll_runner.fetch_daily_revenue",
        fake_fetch_daily_revenue,
    )
    session = RevenueCacheFakeSession()

    first = await ensure_daily_revenue_cached(
        session, date(2026, 5, 19), date(2026, 5, 20)
    )
    second = await ensure_daily_revenue_cached(
        session, date(2026, 5, 19), date(2026, 5, 20)
    )

    assert calls == [(date(2026, 5, 19), date(2026, 5, 20))]
    assert first == {
        date(2026, 5, 19): Decimal("350000"),
        date(2026, 5, 20): Decimal("0"),
    }
    assert second == first
    assert session.setting is not None
    assert session.setting.key == DAILY_REVENUE_CONFIG_KEY
    assert session.setting.value == {"2026-05-19": "350000", "2026-05-20": "0"}
    assert [item for item in session.added if isinstance(item, AppSettingHistory)]


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


def test_tenure_months_calendar() -> None:
    employee = make_employee(hire_date=date(2025, 4, 15), tenure_started_at=date(2025, 4, 15))

    assert tenure_months_on(employee, date(2025, 10, 14)) == 5
    assert tenure_months_on(employee, date(2025, 10, 15)) == 6
    assert tenure_months_on(employee, date(2026, 4, 14)) == 11
    assert tenure_months_on(employee, date(2026, 4, 15)) == 12


def test_fund_rate_by_tenure() -> None:
    settings = payroll_settings()
    settings["payroll.fund_rates_by_tenure"] = [
        {"min_months": 0, "rate": 0},
        {"min_months": 6, "rate": 0.05},
        {"min_months": 12, "rate": 0.10},
        {"min_months": 18, "rate": 0.15},
    ]

    rates = [_fund_rate_for_months(settings, months) for months in (0, 5, 12, 17, 18, 25)]

    assert rates == [
        Decimal("0"),
        Decimal("0"),
        Decimal("0.10"),
        Decimal("0.10"),
        Decimal("0.15"),
        Decimal("0.15"),
    ]
    assert _fund_rate_for_months(payroll_settings(), 6) == Decimal("0.05")


def test_fund_accrual_per_day_uses_today_rate() -> None:
    period = make_period(
        start=date(2025, 10, 14),
        end=date(2025, 10, 15),
        payroll_date=date(2025, 10, 21),
    )
    run_id = uuid.uuid4()
    employee = make_employee(hire_date=date(2025, 4, 15), tenure_started_at=date(2025, 4, 15))
    settings = payroll_settings()
    settings["payroll.fund_rates_by_tenure"] = [
        {"min_months": 0, "rate": 0},
        {"min_months": 6, "rate": 0.05},
        {"min_months": 12, "rate": 0.10},
        {"min_months": 18, "rate": 0.15},
    ]
    entries = [
        make_entry(period, employee, date(2025, 10, 14)),
        make_entry(period, employee, date(2025, 10, 15)),
    ]

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        entries,
        {employee.id: employee},
        settings,
    )

    assert result.blocking_issues == []
    assert result.lines[0].fund_accrual == 110
    assert [day["fund_rate_percent"] for day in result.lines[0].components["days"]] == [0.0, 5.0]
    assert [day["fund_accrual"] for day in result.lines[0].components["days"]] == [0, 110]


def test_fund_accrual_includes_weekday_premium() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee(hire_date=date(2025, 11, 22), tenure_started_at=date(2025, 11, 22))
    entry = make_entry(period, employee, date(2026, 5, 22), minutes=480)

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [entry],
        {employee.id: employee},
        payroll_settings(),
    )

    day = result.lines[0].components["days"][0]
    assert day["weekday_premium"] == 200
    assert Decimal(str(day["base_pay"])) == Decimal("1666.67")
    assert result.lines[0].fund_accrual == 83


def test_substitute_shift_skips_seniority_fund_and_deposit() -> None:
    work_date = date(2026, 5, 22)
    period = make_period(start=work_date, end=work_date, payroll_date=date(2026, 5, 26))
    run_id = uuid.uuid4()
    employee = make_employee(
        position="Управляющий",
        category=None,
        default_cooking_station=None,
        hire_date=date(2025, 1, 1),
        tenure_started_at=date(2025, 1, 1),
    )
    employee.is_senior = True
    settings = deposit_settings()
    settings[EMPLOYEE_ASSIGNMENTS_CONFIG_KEY] = {
        (employee.id, work_date): [
            make_role_assignment(
                employee.id,
                "sushi",
                "category_2",
                is_substitute=True,
            )
        ]
    }

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [make_entry(period, employee, work_date, role="Сушист")],
        {employee.id: employee},
        settings,
    )

    assert result.blocking_issues == []
    line = result.lines[0]
    day = line.components["days"][0]
    assert day["is_substitute"] is True
    assert day["seniority_allowance_pay"] == 0
    assert day["seniority_allowance_skipped_reason"] == "substitute"
    assert day["weekday_premium"] == 200
    assert line.base_pay == 2600
    assert line.fund_accrual == 0
    assert line.components["deposit_withholding"] == 0


async def test_fund_accrual_creates_transaction() -> None:
    period = make_period()
    employee_id = uuid.uuid4()
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=period.id,
        started_at=datetime(2026, 5, 27, tzinfo=UTC),
        status="completed",
        blocking_issues=[],
        summary={},
    )
    line = make_payroll_line(run.id, employee_id)
    session = FundFakeSession()

    total = await accrue_fund(session, period, [line], run)  # type: ignore[arg-type]

    assert total == Decimal("500")
    account = next(iter(session.accounts.values()))
    assert account.accumulated_amount == Decimal("500")
    transaction = session.transactions[0]
    assert transaction.transaction_type == "accrual"
    assert transaction.amount == Decimal("500")
    assert transaction.rate_percent == Decimal("0.05000")
    assert transaction.base_pay_amount == Decimal("10000")
    assert transaction.run_id == run.id


async def test_fund_accrual_skipped_for_non_payroll_position() -> None:
    period = make_period()
    employee = make_employee(
        position="Управляющий",
        category=None,
        hire_date=date(2025, 1, 1),
        tenure_started_at=date(2025, 1, 1),
    )
    settings = payroll_settings()
    assert (
        fund_accrual_for_day(settings, employee, period.end_date, Decimal("10000"))
        == Decimal("0")
    )
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=period.id,
        started_at=datetime(2026, 5, 27, tzinfo=UTC),
        status="completed",
        blocking_issues=[],
        summary={},
    )
    line = make_payroll_line(run.id, employee.id)
    session = FundFakeSession(employees=[employee])

    total = await accrue_fund(session, period, [line], run)  # type: ignore[arg-type]

    assert total == Decimal("0")
    assert session.accounts == {}
    assert session.transactions == []


def test_fund_accrual_zero_for_excluded_indefinitely() -> None:
    employee = make_employee(
        hire_date=date(2025, 1, 1),
        tenure_started_at=date(2025, 1, 1),
    )
    employee.fund_excluded = True
    employee.fund_excluded_until = None

    assert (
        fund_accrual_for_day(payroll_settings(), employee, date(2026, 7, 15), Decimal("10000"))
        == Decimal("0")
    )


def test_fund_accrual_zero_until_excluded_end() -> None:
    employee = make_employee(
        hire_date=date(2025, 1, 1),
        tenure_started_at=date(2025, 1, 1),
    )
    employee.fund_excluded = True
    employee.fund_excluded_until = date(2026, 8, 31)

    blocked = fund_accrual_for_day(
        payroll_settings(), employee, date(2026, 7, 15), Decimal("10000")
    )
    resumed = fund_accrual_for_day(
        payroll_settings(), employee, date(2026, 9, 1), Decimal("10000")
    )

    assert blocked == Decimal("0")
    assert resumed == Decimal("1500")


def test_fund_day_component_marks_excluded_day() -> None:
    period = make_period(start=date(2026, 7, 13), end=date(2026, 7, 19))
    employee = make_employee(
        hire_date=date(2025, 1, 1),
        tenure_started_at=date(2025, 1, 1),
    )
    employee.fund_excluded = True
    employee.fund_excluded_until = None
    entry = make_entry(period, employee, date(2026, 7, 15))

    result = calculate_payroll_lines_from_inputs(
        period,
        uuid.uuid4(),
        [entry],
        {employee.id: employee},
        payroll_settings(),
    )

    day = result.lines[0].components["days"][0]
    assert result.lines[0].fund_accrual == Decimal("0")
    assert day["fund_excluded"] is True


async def test_fund_accrual_idempotent_on_run_resubmit() -> None:
    period = make_period()
    employee_id = uuid.uuid4()
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=period.id,
        started_at=datetime(2026, 5, 27, tzinfo=UTC),
        status="completed",
        blocking_issues=[],
        summary={},
    )
    line = make_payroll_line(run.id, employee_id)
    session = FundFakeSession()

    await accrue_fund(session, period, [line], run)  # type: ignore[arg-type]
    await accrue_fund(session, period, [line], run)  # type: ignore[arg-type]

    account = next(iter(session.accounts.values()))
    assert account.accumulated_amount == Decimal("500")
    assert len(session.transactions) == 1
    assert session.transactions[0].amount == Decimal("500")


async def test_dismiss_forfeits_active_fund() -> None:
    employee = make_employee()
    accounts = [
        AccumulationFundAccount(
            id=uuid.uuid4(),
            employee_id=employee.id,
            year=2025,
            accumulated_amount=Decimal("5000"),
            paid_out_amount=Decimal("5000"),
            forfeited_amount=Decimal("0"),
            status="paid_out",
            paid_out_at=datetime(2026, 1, 15, tzinfo=UTC),
        ),
        AccumulationFundAccount(
            id=uuid.uuid4(),
            employee_id=employee.id,
            year=2026,
            accumulated_amount=Decimal("2000"),
            paid_out_amount=Decimal("0"),
            forfeited_amount=Decimal("0"),
            status="active",
        ),
    ]
    session = FundFakeSession(employees=[employee], accounts=accounts)

    await forfeit_active_fund_on_dismiss(
        session, employee, fire_date=date(2026, 1, 16), now=datetime(2026, 1, 16, tzinfo=UTC)
    )

    assert accounts[0].status == "paid_out"
    assert accounts[1].status == "forfeited"
    assert accounts[1].forfeited_amount == Decimal("2000")
    assert session.transactions[0].transaction_type == "forfeit"


async def test_dismiss_forfeits_all_active_before_payout() -> None:
    employee = make_employee()
    accounts = [
        AccumulationFundAccount(
            id=uuid.uuid4(),
            employee_id=employee.id,
            year=2025,
            accumulated_amount=Decimal("5000"),
            paid_out_amount=Decimal("0"),
            forfeited_amount=Decimal("0"),
            status="active",
        ),
        AccumulationFundAccount(
            id=uuid.uuid4(),
            employee_id=employee.id,
            year=2026,
            accumulated_amount=Decimal("2000"),
            paid_out_amount=Decimal("0"),
            forfeited_amount=Decimal("0"),
            status="active",
        ),
    ]
    session = FundFakeSession(employees=[employee], accounts=accounts)

    await forfeit_active_fund_on_dismiss(
        session, employee, fire_date=date(2026, 1, 14), now=datetime(2026, 1, 14, tzinfo=UTC)
    )

    assert [account.status for account in accounts] == ["forfeited", "forfeited"]
    assert [transaction.year for transaction in session.transactions] == [2025, 2026]


def test_reinstate_resets_tenure_but_not_fund() -> None:
    employee = make_employee(
        status="inactive",
        hire_date=date(2025, 4, 15),
        tenure_started_at=date(2025, 4, 15),
    )
    account = AccumulationFundAccount(
        id=uuid.uuid4(),
        employee_id=employee.id,
        year=2026,
        accumulated_amount=Decimal("2000"),
        paid_out_amount=Decimal("0"),
        forfeited_amount=Decimal("2000"),
        status="forfeited",
    )

    employee.tenure_started_at = date(2026, 6, 15)

    assert tenure_months_on(employee, date(2026, 6, 15)) == 0
    assert account.status == "forfeited"
    assert account.forfeited_amount == Decimal("2000")


async def test_payout_january_15_triggered_by_period() -> None:
    employee_id = uuid.uuid4()
    account = AccumulationFundAccount(
        id=uuid.uuid4(),
        employee_id=employee_id,
        year=2025,
        accumulated_amount=Decimal("5000"),
        paid_out_amount=Decimal("0"),
        forfeited_amount=Decimal("0"),
        status="active",
    )
    period = make_period(
        start=date(2026, 1, 13),
        end=date(2026, 1, 19),
        payroll_date=date(2026, 1, 20),
    )
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=period.id,
        started_at=datetime(2026, 1, 20, tzinfo=UTC),
        status="completed",
        blocking_issues=[],
        summary={},
    )
    session = FundFakeSession(accounts=[account])

    paid = await payout_previous_year_fund_if_due(session, period, run)  # type: ignore[arg-type]
    repeat = await payout_previous_year_fund_if_due(
        session,
        make_period(start=date(2026, 1, 20), end=date(2026, 1, 26), payroll_date=date(2026, 1, 27)),
        run,
    )  # type: ignore[arg-type]

    assert paid == Decimal("5000")
    assert repeat == Decimal("0")
    assert account.status == "paid_out"
    assert session.transactions[0].transaction_type == "payout"


async def test_january_payout_includes_pre_exclusion_accruals() -> None:
    employee = make_employee(
        hire_date=date(2025, 1, 1),
        tenure_started_at=date(2025, 1, 1),
    )
    employee.fund_excluded = True
    employee.fund_excluded_until = None
    account = AccumulationFundAccount(
        id=uuid.uuid4(),
        employee_id=employee.id,
        year=2026,
        accumulated_amount=Decimal("5000"),
        paid_out_amount=Decimal("0"),
        forfeited_amount=Decimal("0"),
        status="active",
    )
    period = make_period(
        start=date(2027, 1, 12),
        end=date(2027, 1, 18),
        payroll_date=date(2027, 1, 19),
    )
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=period.id,
        started_at=datetime(2027, 1, 19, tzinfo=UTC),
        status="completed",
        blocking_issues=[],
        summary={},
    )
    session = FundFakeSession(employees=[employee], accounts=[account])

    paid = await payout_previous_year_fund_if_due(session, period, run)  # type: ignore[arg-type]

    assert paid == Decimal("5000")
    assert account.status == "paid_out"
    assert account.paid_out_amount == Decimal("5000")


def test_manual_payout_endpoint() -> None:
    employee_id = uuid.uuid4()
    account = AccumulationFundAccount(
        id=uuid.uuid4(),
        employee_id=employee_id,
        year=2025,
        accumulated_amount=Decimal("5000"),
        paid_out_amount=Decimal("0"),
        forfeited_amount=Decimal("0"),
        status="active",
    )
    session = FundFakeSession(accounts=[account])
    app = create_app()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/payroll/fund/payout/2025",
                headers={"X-User-Role": "finance_manager"},
            )
            repeat = client.post(
                "/api/v1/payroll/fund/payout/2025",
                headers={"X-User-Role": "finance_manager"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["paid_out_count"] == 1
    assert response.json()["total_paid_out"] == "5000.00"
    assert repeat.status_code == 200
    assert repeat.json()["paid_out_count"] == 0


def test_summary_endpoint() -> None:
    employee_id = uuid.uuid4()
    session = FundFakeSession(
        accounts=[
            AccumulationFundAccount(
                id=uuid.uuid4(),
                employee_id=employee_id,
                year=2026,
                accumulated_amount=Decimal("8000"),
                paid_out_amount=Decimal("1000"),
                forfeited_amount=Decimal("500"),
                status="active",
            )
        ],
        transactions=[
            AccumulationFundTransaction(
                id=uuid.uuid4(),
                account_id=uuid.uuid4(),
                employee_id=employee_id,
                year=2026,
                transaction_type="payout",
                amount=Decimal("1000"),
                created_at=datetime(2026, 1, 15, tzinfo=UTC),
            ),
            AccumulationFundTransaction(
                id=uuid.uuid4(),
                account_id=uuid.uuid4(),
                employee_id=employee_id,
                year=2026,
                transaction_type="forfeit",
                amount=Decimal("500"),
                created_at=datetime(2026, 5, 1, tzinfo=UTC),
            ),
        ],
    )
    app = create_app()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/payroll/fund/summary?year=2026",
                headers={"X-User-Role": "finance_manager"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_outstanding"] == "6500.00"
    assert payload["total_paid_out_ytd"] == "1000.00"
    assert payload["total_forfeited_ytd"] == "500.00"
    assert payload["active_employees_count"] == 1


def deposit_settings(*, withholding: int = 2000) -> dict[str, Any]:
    settings = payroll_settings()
    settings["payroll.deposit_auto_withholding_enabled"] = True
    settings["payroll.category_rules"]["2"]["deposit_withholding"] = withholding
    return settings


def test_deposit_withholding_is_capped_by_target_balance() -> None:
    employee = make_employee(category="category_2")

    deduction = deposit_withholding(
        deposit_settings(),
        employee,
        Decimal("5000"),
        today=date(2026, 5, 31),
        current_balance=Decimal("14000"),
    )

    assert deduction == Decimal("1000")


def test_payroll_calculation_uses_current_deposit_balance_for_target_cap() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee(category="category_2")
    entry = make_entry(period, employee, period.start_date)

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [entry],
        {employee.id: employee},
        deposit_settings(),
        deposit_balances={employee.id: Decimal("14900")},
    )

    assert result.blocking_issues == []
    assert result.lines[0].deduction == 100
    assert result.lines[0].deposit_excluded_for_run is False


def test_payroll_calculation_line_deposit_override_zeroes_only_selected_line() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    first = make_employee(category="category_2")
    second = make_employee(category="category_2")
    entries = [
        make_entry(period, first, period.start_date),
        make_entry(period, second, period.start_date),
    ]

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        entries,
        {first.id: first, second.id: second},
        deposit_settings(),
        line_deposit_overrides={
            (first.id, "Пиццерист"): {
                "deposit_excluded_for_run": True,
                "deposit_exclusion_reason": "Разово без депозита",
            }
        },
    )

    lines_by_employee = {line.employee_id: line for line in result.lines}
    first_line = lines_by_employee[first.id]
    second_line = lines_by_employee[second.id]
    assert first_line.deposit_excluded_for_run is True
    assert first_line.deposit_exclusion_reason == "Разово без депозита"
    assert first_line.components["deposit_withholding"] == 0
    assert first_line.deduction == 0
    assert second_line.deposit_excluded_for_run is False
    assert second_line.components["deposit_withholding"] == 2000
    assert second_line.deduction == 2000


def test_line_deposit_override_mapping_transfers_to_recreated_line() -> None:
    period = make_period()
    run_id = uuid.uuid4()
    employee = make_employee(category="category_2")
    entry = make_entry(period, employee, period.start_date)
    old_line = make_payroll_line(run_id, employee.id)
    old_line.deposit_excluded_for_run = True
    old_line.deposit_exclusion_reason = "Перенести после пересчёта"

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [entry],
        {employee.id: employee},
        deposit_settings(),
        line_deposit_overrides=line_deposit_overrides_from_lines([old_line]),
    )

    assert result.lines[0].deposit_excluded_for_run is True
    assert result.lines[0].deposit_exclusion_reason == "Перенести после пересчёта"
    assert result.lines[0].components["deposit_withholding"] == 0


def test_deposit_withholding_is_zero_when_target_reached() -> None:
    employee = make_employee(category="category_2")

    deduction = deposit_withholding(
        deposit_settings(),
        employee,
        Decimal("5000"),
        today=date(2026, 5, 31),
        current_balance=Decimal("15000"),
    )

    assert deduction == Decimal("0")


def test_employee_deposit_target_override_has_priority() -> None:
    employee = make_employee(category="category_2")
    employee.deposit_target_override = Decimal("20000")

    deduction = deposit_withholding(
        deposit_settings(),
        employee,
        Decimal("5000"),
        today=date(2026, 5, 31),
        current_balance=Decimal("19000"),
    )

    assert employee_deposit_target(deposit_settings(), employee) == Decimal("20000")
    assert deduction == Decimal("1000")


def test_deposit_exclusion_without_until_date_blocks_withholding() -> None:
    employee = make_employee(category="category_2")
    employee.deposit_excluded = True
    employee.deposit_excluded_until = None

    deduction = deposit_withholding(
        deposit_settings(),
        employee,
        Decimal("5000"),
        today=date(2026, 5, 31),
        current_balance=Decimal("0"),
    )

    assert deduction == Decimal("0")


def test_deposit_exclusion_with_until_date_auto_resumes() -> None:
    employee = make_employee(category="category_2")
    employee.deposit_excluded = True
    employee.deposit_excluded_until = date(2026, 6, 10)

    blocked = deposit_withholding(
        deposit_settings(),
        employee,
        Decimal("5000"),
        today=date(2026, 6, 9),
        current_balance=Decimal("0"),
    )
    resumed = deposit_withholding(
        deposit_settings(),
        employee,
        Decimal("5000"),
        today=date(2026, 6, 11),
        current_balance=Decimal("0"),
    )

    assert blocked == Decimal("0")
    assert resumed == Decimal("2000")


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


def test_get_lines_response_enriches_deposit_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app()
    run_id = uuid.uuid4()
    employee_id = uuid.uuid4()
    line = make_payroll_line(
        run_id,
        employee_id,
        components={"days": [], "deposit_withholding": "750.25", "adjustments": {}},
    )
    session = PayrollLineRouteFakeSession([(employee_id, Decimal("1250.50"))])

    async def override_session():
        yield session

    async def fake_get_run_lines(_session: Any, requested_run_id: uuid.UUID) -> list[PayrollLine]:
        assert requested_run_id == run_id
        return [line]

    app.dependency_overrides[get_session] = override_session
    monkeypatch.setattr(payroll_routes, "get_run_lines", fake_get_run_lines)
    try:
        with TestClient(app) as client:
            response = client.get(
                f"/api/v1/payroll/runs/{run_id}/lines",
                headers={"X-User-Role": "finance_manager"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["deposit_withholding"] == 750.25
    assert payload[0]["deposit_payout"] == 1250.5
    assert payload[0]["ndfl_deduction"] == 0
    assert len(session.statements) == 1


def test_get_lines_response_defaults_deposit_payout_and_ndfl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app()
    run_id = uuid.uuid4()
    line = make_payroll_line(run_id, uuid.uuid4())
    session = PayrollLineRouteFakeSession([])

    async def override_session():
        yield session

    async def fake_get_run_lines(_session: Any, _run_id: uuid.UUID) -> list[PayrollLine]:
        return [line]

    app.dependency_overrides[get_session] = override_session
    monkeypatch.setattr(payroll_routes, "get_run_lines", fake_get_run_lines)
    try:
        with TestClient(app) as client:
            response = client.get(
                f"/api/v1/payroll/runs/{run_id}/lines",
                headers={"X-User-Role": "finance_manager"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["deposit_withholding"] == 0
    assert payload[0]["deposit_payout"] == 0
    assert payload[0]["ndfl_deduction"] == 0


def test_personal_report_returns_periods_and_adjustments() -> None:
    app = create_app()
    employee = make_employee()
    employee.full_name = "Иван Тестов"
    employee.position = "Повар"
    first_period = make_period(
        start=date(2026, 5, 19),
        end=date(2026, 5, 25),
        payroll_date=date(2026, 5, 26),
    )
    second_period = make_period(
        start=date(2026, 5, 26),
        end=date(2026, 6, 1),
        payroll_date=date(2026, 6, 2),
    )
    first_run = PayrollRun(
        id=uuid.uuid4(),
        period_id=first_period.id,
        started_at=datetime(2026, 5, 26, tzinfo=UTC),
        status="completed",
        blocking_issues=[],
        summary={},
    )
    second_run = PayrollRun(
        id=uuid.uuid4(),
        period_id=second_period.id,
        started_at=datetime(2026, 6, 2, tzinfo=UTC),
        status="finalized",
        blocking_issues=[],
        summary={},
    )
    first_line = make_payroll_line(
        first_run.id,
        employee.id,
        components={
            "days": [],
            "deposit_withholding": "250",
            "adjustments": {
                "bonuses": [{"amount": "300"}],
                "penalties": [{"amount": "100"}],
            },
        },
    )
    second_line = make_payroll_line(
        second_run.id,
        employee.id,
        components={"days": [], "deposit_withholding": "0", "adjustments": {}},
    )
    bonus = make_adjustment(employee, date(2026, 5, 20), "bonus", Decimal("300"), label="Премия")
    penalty = make_adjustment(
        employee,
        date(2026, 5, 21),
        "penalty",
        Decimal("100"),
        label="Штраф",
    )
    deposit_transaction = DepositTransaction(
        id=uuid.uuid4(),
        employee_id=employee.id,
        run_id=first_run.id,
        transaction_type="accrual",
        amount=Decimal("250"),
        created_at=datetime(2026, 5, 26, 10, 0, tzinfo=UTC),
    )
    session = PersonalReportFakeSession(
        employees=[employee],
        line_rows=[(second_line, second_run, second_period), (first_line, first_run, first_period)],
        adjustments=[bonus, penalty],
        deposit_transactions=[deposit_transaction],
    )

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/payroll/employee-report",
                headers={"X-User-Role": "finance_manager"},
                params={
                    "employee_id": str(employee.id),
                    "date_from": "2026-05-19",
                    "date_to": "2026-06-01",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["employee_id"] == str(employee.id)
    assert payload["employee_name"] == "Иван Тестов"
    assert len(payload["periods"]) == 2
    assert payload["periods"][0]["period_start"] == "2026-05-26"
    assert payload["periods"][1]["bonus_total"] == 300
    assert payload["periods"][1]["penalty_total"] == 100
    assert len(payload["adjustments"]) == 2
    assert payload["totals"]["bonus_total"] == 300
    assert payload["totals"]["penalty_total"] == 100
    assert payload["totals"]["deposit_withholding"] == 250
    assert payload["deposit_transactions"][0]["transaction_type"] == "accrual"


def test_personal_report_date_range_validation() -> None:
    app = create_app()
    employee = make_employee()
    session = PersonalReportFakeSession(employees=[employee])

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/payroll/employee-report",
                headers={"X-User-Role": "finance_manager"},
                params={
                    "employee_id": str(employee.id),
                    "date_from": "2026-06-01",
                    "date_to": "2026-05-19",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json()["detail"] == "date_from must be <= date_to"


def test_personal_report_404_for_unknown_employee() -> None:
    app = create_app()
    session = PersonalReportFakeSession()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/payroll/employee-report",
                headers={"X-User-Role": "finance_manager"},
                params={
                    "employee_id": str(uuid.uuid4()),
                    "date_from": "2026-05-19",
                    "date_to": "2026-06-01",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()["detail"] == "Employee not found"


def test_patch_payroll_line_deposit_override_updates_line_and_audit() -> None:
    app = create_app()
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=uuid.uuid4(),
        started_at=datetime(2026, 5, 27, tzinfo=UTC),
        finished_at=datetime(2026, 5, 27, 1, tzinfo=UTC),
        status="completed",
        blocking_issues=[],
        summary={},
    )
    line = make_payroll_line(
        run.id,
        uuid.uuid4(),
        components={"days": [], "deposit_withholding": "1000", "adjustments": {}},
    )
    session = PayrollLinePatchFakeSession(line, run)

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            response = client.patch(
                f"/api/v1/payroll/lines/{line.id}",
                headers={"X-User-Role": "finance_manager"},
                json={
                    "deposit_excluded_for_run": True,
                    "deposit_exclusion_reason": "Разовая договорённость",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["deposit_excluded_for_run"] is True
    assert response.json()["deposit_exclusion_reason"] == "Разовая договорённость"
    assert line.deposit_excluded_for_run is True
    assert line.deposit_exclusion_reason == "Разовая договорённость"
    action = next(item for item in session.added if isinstance(item, AgentAction))
    assert action.action_type == "payroll_line_deposit_override"
    assert action.target_table == "payroll_line"
    assert action.before_value["deposit_excluded_for_run"] is False
    assert action.after_value["deposit_excluded_for_run"] is True
    assert session.committed is True


def test_patch_payroll_line_deposit_override_rejects_finalized_run() -> None:
    app = create_app()
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=uuid.uuid4(),
        started_at=datetime(2026, 5, 27, tzinfo=UTC),
        finished_at=datetime(2026, 5, 27, 1, tzinfo=UTC),
        status="finalized",
        blocking_issues=[],
        summary={},
    )
    line = make_payroll_line(run.id, uuid.uuid4())
    session = PayrollLinePatchFakeSession(line, run)

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            response = client.patch(
                f"/api/v1/payroll/lines/{line.id}",
                headers={"X-User-Role": "finance_manager"},
                json={"deposit_excluded_for_run": True},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert line.deposit_excluded_for_run is False
    assert [item for item in session.added if isinstance(item, AgentAction)] == []
    assert session.committed is False


class PayrollAdjustmentFakeSession:
    def __init__(
        self,
        employee: Employee,
        *,
        category: PayrollAdjustmentCategory | None = None,
        adjustment: PayrollAdjustment | None = None,
    ) -> None:
        self.employee = employee
        self.category = category
        self.adjustment = adjustment
        self.added: list[Any] = []
        self.committed = False
        self.deleted: list[Any] = []

    async def get(self, model: Any, object_id: uuid.UUID) -> Any:
        if model is Employee and object_id == self.employee.id:
            return self.employee
        if model is PayrollAdjustmentCategory and self.category and object_id == self.category.id:
            return self.category
        if model is PayrollAdjustment and self.adjustment and object_id == self.adjustment.id:
            return self.adjustment
        return None

    def add(self, item: Any) -> None:
        self.added.append(item)
        if isinstance(item, PayrollAdjustment):
            self.adjustment = item

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _item: Any) -> None:
        return None

    async def delete(self, item: Any) -> None:
        self.deleted.append(item)


def app_with_payroll_adjustment_session(session: PayrollAdjustmentFakeSession):
    app = create_app()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    return app


def adjustment_category(adjustment_type: str = "bonus") -> PayrollAdjustmentCategory:
    return PayrollAdjustmentCategory(
        id=uuid.uuid4(),
        type=adjustment_type,
        code=f"{adjustment_type}-{uuid.uuid4()}",
        display_name="Категория",
        default_amount=Decimal("1000"),
        is_active=True,
        sort_order=0,
    )


async def locked_adjustment_date(_session: Any, _work_date: date) -> None:
    raise PayrollAdjustmentLockedError("Период зафиксирован, изменения невозможны")


def test_create_adjustment_in_finalized_period_409(monkeypatch: pytest.MonkeyPatch) -> None:
    employee = make_employee()
    category = adjustment_category("bonus")
    session = PayrollAdjustmentFakeSession(employee, category=category)
    monkeypatch.setattr(payroll_adjustment_routes, "assert_date_not_locked", locked_adjustment_date)

    with TestClient(app_with_payroll_adjustment_session(session)) as client:
        response = client.post(
            "/api/v1/payroll/adjustments",
            headers={"X-User-Role": "finance_manager"},
            json={
                "employee_id": str(employee.id),
                "work_date": "2026-05-20",
                "type": "bonus",
                "category_id": str(category.id),
                "amount": "1000",
            },
        )

    assert response.status_code == 409
    assert session.added == []


def test_patch_delete_in_finalized_period_409(monkeypatch: pytest.MonkeyPatch) -> None:
    employee = make_employee()
    adjustment = make_adjustment(employee, date(2026, 5, 20), "penalty", Decimal("500"))
    session = PayrollAdjustmentFakeSession(employee, adjustment=adjustment)
    monkeypatch.setattr(payroll_adjustment_routes, "assert_date_not_locked", locked_adjustment_date)

    with TestClient(app_with_payroll_adjustment_session(session)) as client:
        patch_response = client.patch(
            f"/api/v1/payroll/adjustments/{adjustment.id}",
            headers={"X-User-Role": "finance_manager"},
            json={"amount": "600"},
        )
        delete_response = client.delete(
            f"/api/v1/payroll/adjustments/{adjustment.id}",
            headers={"X-User-Role": "finance_manager"},
        )

    assert patch_response.status_code == 409
    assert delete_response.status_code == 409
    assert session.deleted == []


def test_create_for_non_payroll_position_422() -> None:
    employee = make_employee(position="Управляющий")
    category = adjustment_category("bonus")
    session = PayrollAdjustmentFakeSession(employee, category=category)

    with TestClient(app_with_payroll_adjustment_session(session)) as client:
        response = client.post(
            "/api/v1/payroll/adjustments",
            headers={"X-User-Role": "finance_manager"},
            json={
                "employee_id": str(employee.id),
                "work_date": "2026-05-20",
                "type": "bonus",
                "category_id": str(category.id),
                "amount": "1000",
            },
        )

    assert response.status_code == 422
    assert session.added == []


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


class FinalizeScalarResult:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def all(self) -> list[Any]:
        return self._items


class FinalizeFakeSession:
    def __init__(
        self,
        run: PayrollRun,
        period: PayrollPeriod,
        *,
        accounts: list[DepositAccount] | None = None,
        transactions: list[DepositTransaction] | None = None,
    ) -> None:
        self.run = run
        self.period = period
        self.accounts = {account.employee_id: account for account in accounts or []}
        self.transactions = transactions or []
        self.added: list[Any] = []
        self.committed = False

    async def get(self, model, object_id):
        if model is PayrollRun and object_id == self.run.id:
            return self.run
        if model is PayrollPeriod and object_id == self.period.id:
            return self.period
        return None

    async def scalars(self, query: Any) -> FinalizeScalarResult:
        entity = query_entity(query)
        if entity is DepositTransaction:
            return FinalizeScalarResult(self.transactions)
        if entity is DepositAccount:
            return FinalizeScalarResult(list(self.accounts.values()))
        return FinalizeScalarResult([])

    def add(self, item: Any) -> None:
        self.added.append(item)
        if isinstance(item, DepositAccount):
            self.accounts[item.employee_id] = item

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _model) -> None:
        return None


def query_entity(query: Any) -> Any | None:
    descriptions = getattr(query, "column_descriptions", None) or []
    if not descriptions:
        return None
    return descriptions[0].get("entity")


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


async def test_finalize_deposit_accrual_updates_balance_not_initial_balance() -> None:
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
    employee_id = uuid.uuid4()
    account = DepositAccount(
        id=uuid.uuid4(),
        employee_id=employee_id,
        balance=Decimal("7000"),
        initial_balance=Decimal("5000"),
        last_updated=datetime(2026, 5, 27, tzinfo=UTC),
    )
    transaction = DepositTransaction(
        id=uuid.uuid4(),
        employee_id=employee_id,
        run_id=run.id,
        transaction_type="accrual",
        amount=Decimal("1000"),
        created_at=datetime(2026, 5, 27, tzinfo=UTC),
    )
    session = FinalizeFakeSession(run, period, accounts=[account], transactions=[transaction])

    await finalize_payroll_run(session, run.id)  # type: ignore[arg-type]

    assert account.balance == Decimal("8000")
    assert account.initial_balance == Decimal("5000")
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


def test_payroll_target_positions_are_cook_and_cashier_only() -> None:
    """Канон таксономии: ЗП считается только для Поваров и Кассиров.

    См. app-spec/modules/staff/taxonomy.md — курьеры, управляющий, менеджер,
    сисадмин, уборщица, посудомойка имеют отдельные правила оплаты.
    """
    assert set(PAYROLL_TARGET_POSITIONS) == {"Повар", "Кассир"}


class AttendanceLoaderFakeScalarResult:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def all(self) -> list[Any]:
        return self._items


class AttendanceLoaderFakeSession:
    """Минимальный fake-session для проверки position-фильтра в load_attendance_entries.

    Возвращает по очереди: пустой список существующих записей → словарь сотрудников.
    """

    def __init__(self, employees: list[Employee], scalar_results: list[Any] | None = None) -> None:
        self._employees = employees
        self._scalar_results = scalar_results or []
        self._scalars_calls = 0
        self.added: list[Any] = []

    async def scalars(self, _stmt: Any) -> AttendanceLoaderFakeScalarResult:
        self._scalars_calls += 1
        if self._scalars_calls == 1:
            return AttendanceLoaderFakeScalarResult([])
        return AttendanceLoaderFakeScalarResult(self._employees)

    async def scalar(self, _stmt: Any) -> Any:
        if self._scalar_results:
            return self._scalar_results.pop(0)
        return None

    def add(self, item: Any) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        return None


async def test_load_attendance_entries_excludes_non_target_positions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    period = make_period()
    cook = make_employee(position="Повар")
    cashier = make_employee(position="Кассир")
    courier = make_employee(position="Курьер")
    manager = make_employee(position="Менеджер")
    dishwasher = make_employee(position="Посудомойка")
    cook.iiko_id = "iiko-cook"
    cashier.iiko_id = "iiko-cashier"
    courier.iiko_id = "iiko-courier"
    manager.iiko_id = "iiko-manager"
    dishwasher.iiko_id = "iiko-dishwasher"

    monkeypatch.setattr(
        "app.services.attendance_loader.load_attendance_rules",
        lambda *_args, **_kwargs: _async_rules(),
    )

    iiko_records = [
        {
            "employeeId": emp.iiko_id,
            "dateFrom": f"2026-05-{20 + i}T09:00:00+03:00",
            "dateTo": f"2026-05-{20 + i}T18:00:00+03:00",
        }
        for i, emp in enumerate([cook, cashier, courier, manager, dishwasher])
    ]

    session = AttendanceLoaderFakeSession([cook, cashier, courier, manager, dishwasher])

    entries = await load_attendance_entries(
        session,  # type: ignore[arg-type]
        period,
        iiko_records=iiko_records,
    )

    employee_ids = {entry.employee_id for entry in entries}
    assert cook.id in employee_ids
    assert cashier.id in employee_ids
    assert courier.id not in employee_ids
    assert manager.id not in employee_ids
    assert dishwasher.id not in employee_ids
    assert len(entries) == 2


async def test_load_attendance_entries_allows_non_target_with_substitute_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    period = make_period()
    manager = make_employee(position="Управляющий", category=None, default_cooking_station=None)
    manager.iiko_id = "iiko-manager-substitute"
    work_date = period.start_date
    ledger_entry = make_shift_ledger_entry(
        manager.id,
        work_date,
        payroll_role="sushi",
        category="category_1",
        is_resolved=True,
    )
    substitute_assignment = make_role_assignment(
        manager.id,
        "sushi",
        "category_1",
        is_substitute=True,
    )

    monkeypatch.setattr(
        "app.services.attendance_loader.load_attendance_rules",
        lambda *_args, **_kwargs: _async_rules(),
    )

    session = AttendanceLoaderFakeSession(
        [manager],
        scalar_results=[ledger_entry, substitute_assignment],
    )

    entries = await load_attendance_entries(
        session,  # type: ignore[arg-type]
        period,
        iiko_records=[
            {
                "employeeId": manager.iiko_id,
                "dateFrom": f"{work_date.isoformat()}T09:00:00+03:00",
                "dateTo": f"{work_date.isoformat()}T18:00:00+03:00",
            }
        ],
    )

    assert [entry.employee_id for entry in entries] == [manager.id]
    assert session.added[0].employee_id == manager.id


async def test_load_attendance_entries_allows_non_target_with_resolved_manual_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    period = make_period()
    manager = make_employee(position="Управляющий", category=None, default_cooking_station=None)
    manager.iiko_id = "iiko-manager-manual-ledger"
    work_date = period.start_date
    ledger_entry = make_shift_ledger_entry(
        manager.id,
        work_date,
        payroll_role="sushi",
        category="category_1",
        is_resolved=True,
    )

    monkeypatch.setattr(
        "app.services.attendance_loader.load_attendance_rules",
        lambda *_args, **_kwargs: _async_rules(),
    )

    session = AttendanceLoaderFakeSession([manager], scalar_results=[ledger_entry])

    entries = await load_attendance_entries(
        session,  # type: ignore[arg-type]
        period,
        iiko_records=[
            {
                "employeeId": manager.iiko_id,
                "dateFrom": f"{work_date.isoformat()}T09:00:00+03:00",
                "dateTo": f"{work_date.isoformat()}T18:00:00+03:00",
            }
        ],
    )

    assert [entry.employee_id for entry in entries] == [manager.id]
    assert session.added[0].employee_id == manager.id


async def _async_rules() -> Any:
    from app.services.attendance_loader import default_attendance_rules

    return default_attendance_rules()

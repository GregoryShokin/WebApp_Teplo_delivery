from __future__ import annotations

import http.client
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.deps import CurrentActor
from app.api.v1.routes import employees as employee_routes
from app.api.v1.routes.employees import (
    change_employee_pin,
    create_employee,
    create_employee_dismissal_reason,
    dismiss_employee,
    list_employee_changes,
    list_employee_dismissal_reasons,
    list_employees,
    patch_employee,
    reinstate_employee,
    set_employee_hire_date,
    update_employee_dismissal_reason,
)
from app.core.security import verify_password
from app.db.session import get_session
from app.main import create_app
from app.models import (
    AccumulationFundAccount,
    AccumulationFundTransaction,
    AgentAction,
    AgentRun,
    DepositAccount,
    DepositTransaction,
    Employee,
    EmployeeChangeEvent,
    EmployeeDismissalReason,
    EmployeePendingIikoAction,
    EmployeePositionEvent,
    EmployeeRoleAssignment,
    ShiftLedgerEntry,
)
from app.schemas.employees import (
    DepositDismissAction,
    EmployeeCreateRequest,
    EmployeeDismissalReasonCreate,
    EmployeeDismissalReasonUpdate,
    EmployeeDismissRequest,
    EmployeeHireDateRequest,
    EmployeePinChangeRequest,
    EmployeeRead,
)
from app.services import iiko_sync as iiko_sync_service
from app.services import notice_service
from app.services.employee_status import compute_status, position_group_for_position
from app.services.iiko_sync import (
    IikoEmployeeCreateResult,
    IikoEmployeeOperationError,
    IikoEmployeeRole,
    IikoEmployeeUpdateResult,
    SyncResult,
    _build_iiko_employee_roles_update_xml,
    _build_iiko_employee_update_xml,
    _enrich_employee_records_with_roles,
    _request_iiko_with_incomplete_read_retry,
    plan_employee_sync,
    sync_employees,
)
from app.services.payroll_calculator import fund_accrual_for_day, tenure_months_on

SYNC_TODAY = date(2026, 5, 27)
SYNC_NOW = datetime(2026, 5, 27, 10, 0, tzinfo=UTC)


class FakeScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


def make_dismissal_reason(
    *,
    code: str,
    label: str,
    requires_comment: bool = False,
    is_active: bool = True,
    sort_order: int = 0,
) -> EmployeeDismissalReason:
    return EmployeeDismissalReason(
        id=uuid.uuid4(),
        code=code,
        label=label,
        requires_comment=requires_comment,
        is_system=True,
        is_active=is_active,
        sort_order=sort_order,
        created_at=SYNC_NOW,
        updated_at=SYNC_NOW,
    )


def make_base_dismissal_reasons() -> list[EmployeeDismissalReason]:
    return [
        make_dismissal_reason(
            code="voluntary",
            label="По собственному желанию",
            sort_order=10,
        ),
        make_dismissal_reason(
            code="no_show",
            label="Не вышел на смену",
            sort_order=20,
        ),
        make_dismissal_reason(
            code="discipline",
            label="Нарушение дисциплины",
            sort_order=30,
        ),
        make_dismissal_reason(
            code="failed_trial",
            label="Не прошёл стажировку",
            sort_order=40,
        ),
        make_dismissal_reason(
            code="layoff_no_shifts",
            label="Сокращение/нет смен",
            sort_order=50,
        ),
        make_dismissal_reason(
            code="transfer",
            label="Перевод",
            sort_order=60,
        ),
        make_dismissal_reason(
            code="other",
            label="Другое",
            requires_comment=True,
            sort_order=70,
        ),
    ]


class FakeSession:
    def __init__(
        self,
        employees: list[Employee],
        shift_entries: list[ShiftLedgerEntry] | None = None,
        change_events: list[EmployeeChangeEvent] | None = None,
        dismissal_reasons: list[EmployeeDismissalReason] | None = None,
        deposit_accounts: list[DepositAccount] | None = None,
        deposit_transactions: list[DepositTransaction] | None = None,
        fund_accounts: list[AccumulationFundAccount] | None = None,
    ) -> None:
        self.employees = employees
        self.shift_entries = shift_entries or []
        self.change_events = change_events or []
        self.dismissal_reasons = dismissal_reasons or make_base_dismissal_reasons()
        self.deposit_accounts = {
            account.employee_id: account for account in (deposit_accounts or [])
        }
        self.deposit_transactions = deposit_transactions or []
        self.fund_accounts = fund_accounts or []
        self.fund_transactions: list[AccumulationFundTransaction] = []
        self.committed = False
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.executed: list[Any] = []
        self.rolled_back = False

    async def get(self, _model: Any, employee_id: uuid.UUID) -> Any | None:
        if _model is EmployeeDismissalReason:
            return next(
                (reason for reason in self.dismissal_reasons if reason.id == employee_id),
                None,
            )
        return next((employee for employee in self.employees if employee.id == employee_id), None)

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def flush(self) -> None:
        for item in self.added:
            if getattr(item, "id", None) is None:
                item.id = uuid.uuid4()

    async def refresh(self, _employee: Employee) -> None:
        return None

    async def delete(self, item: Any) -> None:
        self.deleted.append(item)
        if isinstance(item, Employee) and item in self.employees:
            self.employees.remove(item)

    async def execute(self, query: Any) -> None:
        self.executed.append(query)

    def add(self, item: Any) -> None:
        self.added.append(item)
        if isinstance(item, DepositAccount):
            self.deposit_accounts[item.employee_id] = item
        if isinstance(item, DepositTransaction):
            self.deposit_transactions.append(item)
        if isinstance(item, AccumulationFundTransaction):
            self.fund_transactions.append(item)
        if isinstance(item, Employee) and item not in self.employees:
            self.employees.append(item)
        if isinstance(item, EmployeeRoleAssignment):
            employee = next(
                (employee for employee in self.employees if employee.id == item.employee_id),
                None,
            )
            if employee is not None and item not in employee.role_assignments:
                employee.role_assignments.append(item)
        if isinstance(item, EmployeeChangeEvent) and item not in self.change_events:
            self.change_events.append(item)
        if isinstance(item, EmployeeDismissalReason) and item not in self.dismissal_reasons:
            self.dismissal_reasons.append(item)

    async def scalar(self, _query: Any) -> Any | None:
        sql = str(_query.compile(compile_kwargs={"literal_binds": True}))
        if "employee_dismissal_reason" in sql:
            return next(
                (
                    reason
                    for reason in self.dismissal_reasons
                    if reason.code in sql or str(reason.id) in sql
                ),
                None,
            )
        if "deposit_account" in sql:
            return next(
                (
                    account
                    for employee_id, account in self.deposit_accounts.items()
                    if employee_id.hex in sql or str(employee_id) in sql
                ),
                None,
            )
        return None

    async def scalars(self, query: Any) -> FakeScalarResult:
        sql = str(query.compile(compile_kwargs={"literal_binds": True}))
        if "deposit_transaction" in sql:
            return FakeScalarResult(list(self.deposit_transactions))
        if "accumulation_fund_account" in sql:
            accounts = list(self.fund_accounts)
            if "status = 'active'" in sql:
                accounts = [account for account in accounts if account.status == "active"]
            return FakeScalarResult(accounts)
        if "deposit_account" in sql:
            return FakeScalarResult(list(self.deposit_accounts.values()))
        if "employee_dismissal_reason" in sql:
            reasons = sorted(
                self.dismissal_reasons,
                key=lambda reason: (reason.sort_order, reason.label),
            )
            return FakeScalarResult(reasons)
        if "employee_change_event" in sql:
            events = list(self.change_events)
            if "employee_change_event.source != 'system_migration'" in sql:
                events = [event for event in events if event.source != "system_migration"]
            for field, value_getter in (
                (
                    "employee_id",
                    lambda event: event.employee_id.hex if event.employee_id else "",
                ),
                ("source", lambda event: event.source),
                ("status", lambda event: event.status),
                ("change_type", lambda event: event.change_type),
            ):
                marker = f"employee_change_event.{field} = "
                if marker not in sql:
                    continue
                events = [event for event in events if value_getter(event) in sql]
            events.sort(key=lambda event: event.changed_at, reverse=True)
            return FakeScalarResult(events)
        if "shift_ledger_entry" in sql:
            entries = [entry for entry in self.shift_entries if entry.closed_at is None]
            return FakeScalarResult(entries)
        if "payroll_role_category_availability" in sql:
            return FakeScalarResult([])
        if "employee_role_assignment" in sql:
            assignments = [
                assignment
                for employee in self.employees
                for assignment in employee.role_assignments
                if assignment.effective_to is None or assignment.effective_to > date.today()
            ]
            assignments.sort(
                key=lambda assignment: (not assignment.is_primary, assignment.payroll_role)
            )
            return FakeScalarResult(assignments)

        employees = list(self.employees)
        if "employee.status = 'requires_setup'" in sql:
            employees = [employee for employee in employees if employee.status == "requires_setup"]
        if "employee.status = 'active'" in sql:
            employees = [employee for employee in employees if employee.status == "active"]
        if "employee.status = 'inactive'" in sql:
            employees = [employee for employee in employees if employee.status == "inactive"]
        employees.sort(key=lambda employee: employee.full_name)
        return FakeScalarResult(employees)


def make_employee(
    *,
    iiko_id: str,
    full_name: str,
    status: str = "active",
    position: str | None = "Повар",
    category: str | None = "category_2",
    default_cooking_station: str | None = "sushi",
    fire_date: date | None = None,
    fire_reason: str | None = None,
    pin_hash: str | None = "hashed-pin",
    pin_assumed_from_iiko: bool = False,
) -> Employee:
    return Employee(
        id=uuid.uuid4(),
        iiko_id=iiko_id,
        full_name=full_name,
        status=status,
        position=position,
        category=category,
        default_cooking_station=default_cooking_station,
        fire_date=fire_date,
        fire_reason=fire_reason,
        pin_hash=pin_hash,
        pin_assumed_from_iiko=pin_assumed_from_iiko,
        requires_role_review=False,
        role_review_payload=None,
        pin_set_at=SYNC_NOW if pin_hash else None,
        is_senior=False,
        is_deputy_senior=False,
        created_at=SYNC_NOW,
        updated_at=SYNC_NOW,
    )


def years_ago(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(month=2, day=28, year=value.year - years)


def make_assignment(employee: Employee, payroll_role: str = "sushi") -> EmployeeRoleAssignment:
    return EmployeeRoleAssignment(
        id=uuid.uuid4(),
        employee_id=employee.id,
        payroll_role=payroll_role,
        category="category_2",
        is_primary=True,
        is_substitute=False,
        effective_from=SYNC_TODAY,
        created_at=SYNC_NOW,
        updated_at=SYNC_NOW,
    )


def make_deposit_account(employee: Employee, balance: Decimal) -> DepositAccount:
    return DepositAccount(
        id=uuid.uuid4(),
        employee_id=employee.id,
        balance=balance,
        last_updated=SYNC_NOW,
    )


def make_notice_event(
    employee: Employee,
    notice_date: date,
    *,
    change_type: str = "notice_given",
    changed_at: datetime = SYNC_NOW,
) -> EmployeeChangeEvent:
    return EmployeeChangeEvent(
        id=uuid.uuid4(),
        employee_id=employee.id,
        changed_at=changed_at,
        effective_from=notice_date,
        change_type=change_type,
        source="app",
        actor_label="finance_manager",
        status="success",
        summary="Сотрудник уведомил об уходе",
        payroll_impact=False,
        payroll_impact_metadata={},
        created_at=changed_at,
    )


def test_sync_skips_employee_without_position() -> None:
    existing: dict[str, Employee] = {}

    plan = plan_employee_sync(
        existing,
        [{"id": "iiko-1", "name": "Новый Сотрудник"}],
        SYNC_TODAY,
        SYNC_NOW,
    )

    assert plan.result.created == 0
    assert existing == {}


def test_sync_skips_employee_without_name() -> None:
    existing: dict[str, Employee] = {}

    plan = plan_employee_sync(
        existing,
        [{"id": "iiko-1", "position": "Повар"}],
        SYNC_TODAY,
        SYNC_NOW,
    )

    assert plan.result.created == 0
    assert existing == {}
    assert plan.skipped_records[0].reason == "missing_name_or_position"


def test_sync_skips_employee_outside_target_positions() -> None:
    existing: dict[str, Employee] = {}

    plan = plan_employee_sync(
        existing,
        [{"id": "iiko-1", "name": "Официант", "position": "Официант"}],
        SYNC_TODAY,
        SYNC_NOW,
    )

    assert plan.result.created == 0
    assert existing == {}
    assert plan.skipped_records[0].reason == "non_canonical_or_unsupported_position"


def test_sync_skips_uuid_name_fallback_even_with_target_position() -> None:
    existing: dict[str, Employee] = {}
    iiko_id = str(uuid.uuid4())

    plan = plan_employee_sync(
        existing,
        [{"id": iiko_id, "name": iiko_id, "position": "Повар"}],
        SYNC_TODAY,
        SYNC_NOW,
    )

    assert plan.result.created == 0
    assert existing == {}


def test_sync_skips_empty_name_fields_even_with_target_position() -> None:
    existing: dict[str, Employee] = {}

    plan = plan_employee_sync(
        existing,
        [
            {
                "id": "iiko-empty-name",
                "customUserName": " ",
                "firstName": "",
                "lastName": "\xa0",
                "name": "",
                "position": "Повар",
            }
        ],
        SYNC_TODAY,
        SYNC_NOW,
    )

    assert plan.result.created == 0
    assert existing == {}


def test_sync_creates_target_position_employee_as_requires_setup() -> None:
    existing: dict[str, Employee] = {}

    plan = plan_employee_sync(
        existing,
        [{"id": "iiko-1", "name": "Новый Сотрудник", "position": "Повар"}],
        SYNC_TODAY,
        SYNC_NOW,
    )

    employee = existing["iiko-1"]
    assert plan.result.created == 1
    assert employee.full_name == "Новый Сотрудник"
    assert employee.status == "requires_setup"
    assert employee.position == "Повар"
    assert employee.category is None
    assert employee.default_cooking_station is None
    assert employee.is_senior is False
    assert employee.pin_hash is None
    assert employee.pin_assumed_from_iiko is True


@pytest.mark.parametrize("position", ["Уборщица", "Посудомойка"])
def test_sync_creates_auxiliary_position_employee_as_active_without_pin(position: str) -> None:
    existing: dict[str, Employee] = {}

    plan = plan_employee_sync(
        existing,
        [{"id": f"iiko-{position}", "name": f"{position} Тест", "position": position}],
        SYNC_TODAY,
        SYNC_NOW,
    )

    employee = existing[f"iiko-{position}"]
    assert plan.result.created == 1
    assert employee.position == position
    assert employee.status == "active"
    assert employee.pin_hash is None
    assert employee.category is None
    assert employee.default_cooking_station is None


def test_sync_fills_hire_date_from_iiko_hire_date() -> None:
    existing: dict[str, Employee] = {}

    plan = plan_employee_sync(
        existing,
        [
            {
                "id": "iiko-hire-date",
                "name": "Дата Найма",
                "position": "Повар",
                "hireDate": "2026-05-12T09:30:00",
            }
        ],
        SYNC_TODAY,
        SYNC_NOW,
    )

    assert plan.result.created == 1
    assert existing["iiko-hire-date"].hire_date == date(2026, 5, 12)


def test_sync_canonicalizes_lowercase_target_position_employee() -> None:
    existing: dict[str, Employee] = {}

    plan = plan_employee_sync(
        existing,
        [{"id": "iiko-1", "name": "Повар", "position": "повар"}],
        SYNC_TODAY,
        SYNC_NOW,
    )

    assert plan.result.created == 1
    assert existing["iiko-1"].position == "Повар"


def test_sync_skips_cashier_fastfood_position() -> None:
    existing: dict[str, Employee] = {}

    plan = plan_employee_sync(
        existing,
        [{"id": "iiko-1", "name": "Фастфуд Сотрудник", "position": "Кассир-фастфуда"}],
        SYNC_TODAY,
        SYNC_NOW,
    )

    assert plan.result.created == 0
    assert existing == {}


def test_sync_resolves_position_from_iiko_main_role_id() -> None:
    existing: dict[str, Employee] = {}
    records = _enrich_employee_records_with_roles(
        [{"id": "iiko-1", "name": "Ролевой Сотрудник", "mainRoleId": "role-cook"}],
        [{"id": "role-cook", "name": "Повар"}],
    )

    plan = plan_employee_sync(existing, records, SYNC_TODAY, SYNC_NOW)

    assert plan.result.created == 1
    assert existing["iiko-1"].position == "Повар"


def test_sync_update_preserves_local_pin_hash_and_origin_flag_after_initial_import() -> None:
    existing: dict[str, Employee] = {}

    first = plan_employee_sync(
        existing,
        [{"id": "iiko-local-pin", "name": "Локальный Пин", "position": "Повар"}],
        SYNC_TODAY,
        SYNC_NOW,
    )
    employee = existing["iiko-local-pin"]
    employee.pin_hash = "local-hash"
    employee.pin_assumed_from_iiko = False
    employee.pin_set_at = SYNC_NOW

    second = plan_employee_sync(
        existing,
        [{"id": "iiko-local-pin", "name": "Локальный Пин Обновлён", "position": "Повар"}],
        SYNC_TODAY,
        SYNC_NOW + timedelta(minutes=1),
    )

    assert first.result.created == 1
    assert second.result.updated == 1
    assert employee.full_name == "Локальный Пин Обновлён"
    assert employee.pin_hash == "local-hash"
    assert employee.pin_assumed_from_iiko is False
    assert employee.pin_set_at == SYNC_NOW


def test_sync_skips_service_account() -> None:
    existing: dict[str, Employee] = {}

    plan = plan_employee_sync(
        existing,
        [
            {
                "id": "iiko-service",
                "code": "dxbx168759",
                "name": "DocsInBox User (dxbx168759)",
                "position": "Повар",
            }
        ],
        SYNC_TODAY,
        SYNC_NOW,
    )

    assert plan.result.created == 0
    assert existing == {}


async def test_sync_logs_skipped_record_without_name() -> None:
    session = FakeSession([])
    iiko_id = str(uuid.uuid4())

    result = await sync_employees(
        session,  # type: ignore[arg-type]
        iiko_records=[{"id": iiko_id, "position": "Повар"}],
        today=SYNC_TODAY,
        now=SYNC_NOW,
    )

    actions = [item for item in session.added if isinstance(item, AgentAction)]
    events = [item for item in session.added if isinstance(item, EmployeeChangeEvent)]
    assert result.as_dict() == {"created": 0, "updated": 0, "deactivated": 0}
    assert actions[0].action_type == "skipped: missing_name_or_position"
    assert actions[0].after_value == {
        "iiko_id": iiko_id,
        "reason": "missing_name_or_position",
    }
    assert events[0].change_type == "iiko_sync_skipped"
    assert events[0].status == "skipped"
    assert events[0].source == "iiko_sync"


async def test_sync_logs_noncanonical_position_as_skipped_not_error() -> None:
    session = FakeSession([])

    result = await sync_employees(
        session,  # type: ignore[arg-type]
        iiko_records=[{"id": "iiko-waiter", "name": "Лишний Официант", "position": "Официант"}],
        today=SYNC_TODAY,
        now=SYNC_NOW,
    )

    events = [item for item in session.added if isinstance(item, EmployeeChangeEvent)]
    assert result.as_dict() == {"created": 0, "updated": 0, "deactivated": 0}
    assert events[0].change_type == "iiko_sync_skipped"
    assert events[0].status == "skipped"
    assert events[0].reason == "non_canonical_or_unsupported_position"


async def test_list_employee_changes_filters_by_employee_source_status_type() -> None:
    employee_id = uuid.uuid4()
    other_employee_id = uuid.uuid4()
    matching = EmployeeChangeEvent(
        id=uuid.uuid4(),
        employee_id=employee_id,
        changed_at=datetime(2026, 5, 29, 12, 0, tzinfo=UTC),
        change_type="dismiss",
        source="app",
        actor_label="finance_manager",
        status="success",
        summary="Сотрудник уволен",
        payroll_impact=True,
        payroll_impact_metadata={},
        created_at=datetime(2026, 5, 29, 12, 0, tzinfo=UTC),
    )
    session = FakeSession(
        [],
        change_events=[
            matching,
            EmployeeChangeEvent(
                id=uuid.uuid4(),
                employee_id=employee_id,
                changed_at=datetime(2026, 5, 29, 13, 0, tzinfo=UTC),
                change_type="dismiss",
                source="iiko_sync",
                actor_label="Синхронизация IIko",
                status="success",
                summary="Синхронизация IIko: сотрудник деактивирован",
                payroll_impact=True,
                payroll_impact_metadata={},
                created_at=datetime(2026, 5, 29, 13, 0, tzinfo=UTC),
            ),
            EmployeeChangeEvent(
                id=uuid.uuid4(),
                employee_id=other_employee_id,
                changed_at=datetime(2026, 5, 29, 14, 0, tzinfo=UTC),
                change_type="dismiss",
                source="app",
                actor_label="finance_manager",
                status="error",
                summary="Ошибка",
                payroll_impact=True,
                payroll_impact_metadata={},
                created_at=datetime(2026, 5, 29, 14, 0, tzinfo=UTC),
            ),
        ],
    )

    rows = await list_employee_changes(
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"manager"})),
        employee_id=employee_id,
        change_type="dismiss",
        source="app",
        event_status="success",
    )

    assert rows == [matching]


async def test_list_employee_dismissal_reasons_returns_active_seeded_reasons() -> None:
    inactive = make_dismissal_reason(
        code="season_end",
        label="Сезон завершён",
        is_active=False,
        sort_order=80,
    )
    session = FakeSession([], dismissal_reasons=[*make_base_dismissal_reasons(), inactive])

    rows = await list_employee_dismissal_reasons(
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"manager"})),
    )

    assert [row.label for row in rows] == [
        "По собственному желанию",
        "Не вышел на смену",
        "Нарушение дисциплины",
        "Не прошёл стажировку",
        "Сокращение/нет смен",
        "Перевод",
        "Другое",
    ]
    assert rows[-1].requires_comment is True


async def test_create_employee_dismissal_reason() -> None:
    session = FakeSession([])

    reason = await create_employee_dismissal_reason(
        EmployeeDismissalReasonCreate(label="Переезд", sort_order=80),
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    assert reason in session.dismissal_reasons
    assert reason.code.startswith("custom_")
    assert reason.label == "Переезд"
    assert reason.sort_order == 80
    assert reason.is_active is True
    assert session.committed is True


async def test_update_employee_dismissal_reason() -> None:
    reason = make_dismissal_reason(code="relocation", label="Переезд", sort_order=80)
    session = FakeSession([], dismissal_reasons=[reason])

    updated = await update_employee_dismissal_reason(
        reason.id,
        EmployeeDismissalReasonUpdate(
            label="Переезд в другой город",
            requires_comment=True,
            is_active=False,
            sort_order=90,
        ),
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    assert updated.label == "Переезд в другой город"
    assert updated.requires_comment is True
    assert updated.is_active is False
    assert updated.sort_order == 90
    assert session.committed is True


def test_sync_updates_full_name_for_existing_employee() -> None:
    existing = {
        "iiko-2": make_employee(iiko_id="iiko-2", full_name="Старое Имя"),
    }

    plan = plan_employee_sync(
        existing,
        [{"id": "iiko-2", "fullName": "Новое Имя", "position": "Повар"}],
        SYNC_TODAY,
        SYNC_NOW,
    )

    assert plan.result.updated == 1
    assert existing["iiko-2"].full_name == "Новое Имя"
    assert plan.mutations[0].action_type == "update"


def test_reset_preserves_manual_fields_and_assignments_for_existing_employee() -> None:
    employee = make_employee(
        iiko_id="iiko-manual",
        full_name="Старое Имя",
        status="inactive",
        category="category_3",
        default_cooking_station="pizza",
        fire_date=date(2026, 5, 1),
        fire_reason="manual decision",
    )
    employee.is_senior = True
    employee.is_deputy_senior = True
    assignment = make_assignment(employee, payroll_role="pizza")
    employee.role_assignments.append(assignment)
    existing = {"iiko-manual": employee}

    plan = plan_employee_sync(
        existing,
        [{"id": "iiko-manual", "fullName": "Новое Имя", "position": "Повар"}],
        SYNC_TODAY,
        SYNC_NOW,
        mode="reset",
    )

    assert plan.result.updated == 1
    assert employee.full_name == "Новое Имя"
    assert employee.category == "category_3"
    assert employee.default_cooking_station == "pizza"
    assert employee.is_senior is True
    assert employee.is_deputy_senior is True
    assert employee.status == "inactive"
    assert employee.fire_date == date(2026, 5, 1)
    assert employee.fire_reason == "manual decision"
    assert employee.role_assignments == [assignment]


def test_sync_deactivates_fired_employee() -> None:
    existing = {
        "iiko-3": make_employee(iiko_id="iiko-3", full_name="Активный Сотрудник"),
    }

    plan = plan_employee_sync(
        existing,
        [{"id": "iiko-3", "name": "Активный Сотрудник", "deleted": "true"}],
        SYNC_TODAY,
        SYNC_NOW,
    )

    assert plan.result.deactivated == 1
    assert existing["iiko-3"].status == "inactive"
    assert existing["iiko-3"].fire_date == SYNC_TODAY
    assert plan.mutations[0].action_type == "deactivate"


def test_sync_deactivates_existing_employee_outside_target_positions() -> None:
    existing = {
        "iiko-10": make_employee(iiko_id="iiko-10", full_name="Бывший Повар"),
    }

    plan = plan_employee_sync(
        existing,
        [{"id": "iiko-10", "name": "Бывший Повар", "position": "Официант"}],
        SYNC_TODAY,
        SYNC_NOW,
    )

    assert plan.result.deactivated == 1
    assert existing["iiko-10"].status == "inactive"
    assert existing["iiko-10"].fire_date == SYNC_TODAY


def test_reset_deactivates_employee_missing_from_iiko_preserving_assignments() -> None:
    employee = make_employee(iiko_id="iiko-missing", full_name="Пропавший Повар")
    assignment = make_assignment(employee)
    employee.role_assignments.append(assignment)
    existing = {"iiko-missing": employee}

    plan = plan_employee_sync(existing, [], SYNC_TODAY, SYNC_NOW, mode="reset")

    assert plan.result.deactivated == 1
    assert employee.status == "inactive"
    assert employee.fire_date == SYNC_TODAY
    assert employee.role_assignments == [assignment]
    assert plan.mutations[0].note == "missing from iiko reset"


def test_incremental_does_not_deactivate_employee_missing_from_iiko() -> None:
    employee = make_employee(iiko_id="iiko-still-present", full_name="Не Трогать")
    existing = {"iiko-still-present": employee}

    plan = plan_employee_sync(existing, [], SYNC_TODAY, SYNC_NOW)

    assert plan.result.deactivated == 0
    assert employee.status == "active"
    assert employee.fire_date is None


def test_sync_deactivates_existing_ghost_employee() -> None:
    ghost = make_employee(
        iiko_id="iiko-ghost",
        full_name=str(uuid.uuid4()),
        status="requires_setup",
        position=None,
        category=None,
        default_cooking_station=None,
    )
    existing = {"iiko-ghost": ghost}

    plan = plan_employee_sync(existing, [], SYNC_TODAY, SYNC_NOW)

    assert plan.result.deactivated == 1
    assert ghost.status == "inactive"
    assert ghost.fire_date == SYNC_TODAY
    assert plan.mutations[0].note == "ghost cleanup auto"


async def test_reset_deletes_ghost_without_real_payroll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ghost = make_employee(
        iiko_id="iiko-ghost-delete",
        full_name=str(uuid.uuid4()),
        status="requires_setup",
        position=None,
        category=None,
        default_cooking_station=None,
    )
    session = FakeSession([ghost])

    async def fake_real_payroll_ids(_session: Any, _employee_ids: Any) -> set[uuid.UUID]:
        return set()

    monkeypatch.setattr(
        iiko_sync_service,
        "_employee_ids_with_real_payroll_lines",
        fake_real_payroll_ids,
    )

    result = await sync_employees(
        session,  # type: ignore[arg-type]
        iiko_records=[],
        mode="reset",
        today=SYNC_TODAY,
        now=SYNC_NOW,
    )

    agent_run = next(item for item in session.added if isinstance(item, AgentRun))
    assert ghost not in session.employees
    assert result.ghost_cleaned == 1
    assert agent_run.result["ghost_cleaned"] == 1
    assert agent_run.result["existing_assignments_preserved"] is True


async def test_reset_keeps_ghost_with_real_payroll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ghost = make_employee(
        iiko_id="iiko-ghost-history",
        full_name=str(uuid.uuid4()),
        status="requires_setup",
        position=None,
        category=None,
        default_cooking_station=None,
    )
    session = FakeSession([ghost])

    async def fake_real_payroll_ids(_session: Any, _employee_ids: Any) -> set[uuid.UUID]:
        return {ghost.id}

    monkeypatch.setattr(
        iiko_sync_service,
        "_employee_ids_with_real_payroll_lines",
        fake_real_payroll_ids,
    )

    result = await sync_employees(
        session,  # type: ignore[arg-type]
        iiko_records=[],
        mode="reset",
        today=SYNC_TODAY,
        now=SYNC_NOW,
    )

    assert ghost in session.employees
    assert ghost.status == "inactive"
    assert ghost.fire_date == SYNC_TODAY
    assert result.ghost_cleaned == 0


async def test_patch_employee_full_name_calls_iiko_before_local_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    employee = make_employee(iiko_id="iiko-4", full_name="Readonly Name")
    session = FakeSession([employee])
    calls: list[tuple[str, str | None, str | None]] = []

    async def fake_update_iiko(
        _session: Any,
        *,
        iiko_id: str,
        full_name: str | None = None,
        position: str | None = None,
    ) -> IikoEmployeeUpdateResult:
        assert employee.full_name == "Readonly Name"
        calls.append((iiko_id, full_name, position))
        return IikoEmployeeUpdateResult(
            iiko_id=iiko_id,
            full_name=full_name,
            position=position,
            role_id=None,
            role_code=None,
        )

    monkeypatch.setattr(employee_routes, "update_iiko_employee", fake_update_iiko)

    updated = await patch_employee(
        employee.id,
        {"full_name": "Manual Name"},
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    assert calls == [("iiko-4", "Manual Name", None)]
    assert updated.full_name == "Manual Name"
    assert session.committed is True


async def test_patch_employee_full_name_iiko_error_keeps_local_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    employee = make_employee(iiko_id="iiko-4-error", full_name="Original Name")
    session = FakeSession([employee])

    async def fake_update_iiko(
        _session: Any,
        *,
        iiko_id: str,
        full_name: str | None = None,
        position: str | None = None,
    ) -> IikoEmployeeUpdateResult:
        assert iiko_id == "iiko-4-error"
        assert full_name == "Rejected Name"
        assert position is None
        raise IikoEmployeeOperationError("iiko отклонил имя", status_code=502)

    monkeypatch.setattr(employee_routes, "update_iiko_employee", fake_update_iiko)

    with pytest.raises(HTTPException) as exc_info:
        await patch_employee(
            employee.id,
            {"full_name": "Rejected Name"},
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"finance_manager"})),
        )

    assert exc_info.value.status_code == 502
    assert employee.full_name == "Original Name"
    assert session.committed is False


async def test_patch_employee_category_manager_returns_403() -> None:
    session = FakeSession([make_employee(iiko_id="iiko-5", full_name="Manager Forbidden")])

    with pytest.raises(HTTPException) as exc_info:
        await patch_employee(
            session.employees[0].id,
            {"category": "category_3"},
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"manager"})),
        )

    assert exc_info.value.status_code == 403


async def test_patch_employee_category_finance_manager_ok() -> None:
    employee = make_employee(
        iiko_id="iiko-6",
        full_name="Needs Setup",
        status="requires_setup",
        position="Повар",
        category=None,
        default_cooking_station=None,
    )
    session = FakeSession([employee])

    updated = await patch_employee(
        employee.id,
        {"category": "category_2"},
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    assert updated.category == "category_2"
    assert updated.status == "requires_setup"
    assert session.committed is True


async def test_patch_employee_cooking_station_finishes_cook_setup() -> None:
    employee = make_employee(
        iiko_id="iiko-11",
        full_name="Cook Setup",
        status="requires_setup",
        position="Повар",
        category="category_1",
        default_cooking_station=None,
    )
    session = FakeSession([employee])

    updated = await patch_employee(
        employee.id,
        {"default_cooking_station": "sushi"},
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    assert updated.default_cooking_station == "sushi"
    assert updated.status == "active"


async def test_patch_employee_category_null_requires_setup() -> None:
    employee = make_employee(
        iiko_id="iiko-12",
        full_name="Configured Cook",
        status="active",
        position="Повар",
        category="category_1",
        default_cooking_station="sushi",
    )
    session = FakeSession([employee])

    updated = await patch_employee(
        employee.id,
        {"category": None},
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    assert updated.category is None
    assert updated.status == "requires_setup"


async def test_patch_employee_cashier_category_makes_active_without_station() -> None:
    employee = make_employee(
        iiko_id="iiko-13",
        full_name="Cashier Setup",
        status="requires_setup",
        position="Кассир",
        category=None,
        default_cooking_station=None,
    )
    session = FakeSession([employee])

    updated = await patch_employee(
        employee.id,
        {"category": "category_2"},
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    assert updated.category == "category_2"
    assert updated.default_cooking_station is None
    assert updated.status == "active"


async def test_patch_position_cook_to_cashier_updates_iiko_and_rebuilds_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    employee = make_employee(
        iiko_id="iiko-cook-to-cashier",
        full_name="Role Change",
        status="active",
        position="Повар",
        category="category_2",
        default_cooking_station="sushi",
    )
    cook_assignment = make_assignment(employee, payroll_role="sushi")
    employee.role_assignments.append(cook_assignment)
    session = FakeSession([employee])
    calls: list[tuple[str, str | None, str | None]] = []

    async def fake_update_iiko(
        _session: Any,
        *,
        iiko_id: str,
        full_name: str | None = None,
        position: str | None = None,
    ) -> IikoEmployeeUpdateResult:
        assert employee.position == "Повар"
        calls.append((iiko_id, full_name, position))
        return IikoEmployeeUpdateResult(
            iiko_id=iiko_id,
            full_name=full_name,
            position=position,
            role_id="role-cashier",
            role_code="CA",
        )

    monkeypatch.setattr(employee_routes, "update_iiko_employee", fake_update_iiko)

    updated = await patch_employee(
        employee.id,
        {"position": "Кассир", "category": "category_2"},
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    assignments = [item for item in session.added if isinstance(item, EmployeeRoleAssignment)]
    assert calls == [("iiko-cook-to-cashier", None, "Кассир")]
    assert updated.position == "Кассир"
    assert updated.category == "category_2"
    assert updated.default_cooking_station is None
    assert updated.status == "active"
    assert cook_assignment.effective_to == date.today()
    assert len(assignments) == 1
    assert assignments[0].payroll_role == "administrator"
    assert assignments[0].category == "category_2"
    assert assignments[0].is_primary is True


async def test_future_position_change_schedules_iiko_without_immediate_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    employee = make_employee(
        iiko_id="iiko-future-position",
        full_name="Future Position",
        status="active",
        position="Повар",
        category="category_2",
        default_cooking_station="sushi",
    )
    session = FakeSession([employee])
    calls: list[str] = []

    async def fake_update_iiko(
        _session: Any,
        *,
        iiko_id: str,
        full_name: str | None = None,
        position: str | None = None,
    ) -> IikoEmployeeUpdateResult:
        calls.append(iiko_id)
        return IikoEmployeeUpdateResult(
            iiko_id=iiko_id,
            full_name=full_name,
            position=position,
            role_id=None,
            role_code=None,
        )

    monkeypatch.setattr(employee_routes, "update_iiko_employee", fake_update_iiko)

    updated = await patch_employee(
        employee.id,
        {"position": "Кассир", "effective_from": date.today() + timedelta(days=7)},
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    position_events = [item for item in session.added if isinstance(item, EmployeePositionEvent)]
    pending_actions = [
        item for item in session.added if isinstance(item, EmployeePendingIikoAction)
    ]
    change_events = [item for item in session.added if isinstance(item, EmployeeChangeEvent)]
    assert calls == []
    assert updated.position == "Повар"
    assert position_events[0].position == "Кассир"
    assert pending_actions[0].action_type == "update_position"
    assert pending_actions[0].payload["position"] == "Кассир"
    assert any(event.change_type == "update_position" for event in change_events)


async def test_patch_employee_cashier_cooking_station_returns_400() -> None:
    employee = make_employee(
        iiko_id="iiko-14",
        full_name="Cashier",
        status="active",
        position="Кассир",
        category="category_2",
        default_cooking_station=None,
    )
    session = FakeSession([employee])

    with pytest.raises(HTTPException) as exc_info:
        await patch_employee(
            employee.id,
            {"cooking_station": "sushi"},
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"finance_manager"})),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Цех допустим только для поваров"


async def test_patch_employee_rejects_deputy_senior_for_courier() -> None:
    employee = make_employee(
        iiko_id="iiko-courier-premium",
        full_name="Courier",
        status="active",
        position="Курьер",
        category=None,
        default_cooking_station=None,
    )
    session = FakeSession([employee])

    with pytest.raises(HTTPException) as exc_info:
        await patch_employee(
            employee.id,
            {"is_deputy_senior": True},
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"finance_manager"})),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Надбавка «Зам старшего» недоступна для этой должности"


async def test_change_employee_pin_hashes_and_sets_timestamp() -> None:
    employee = make_employee(iiko_id="iiko-pin", full_name="Pin Employee")
    old_hash = employee.pin_hash
    session = FakeSession([employee])

    updated = await change_employee_pin(
        employee.id,
        EmployeePinChangeRequest(pin_code="9876"),
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    actions = [item for item in session.added if isinstance(item, AgentAction)]
    events = [item for item in session.added if isinstance(item, EmployeeChangeEvent)]
    assert updated.pin_hash is not None
    assert updated.pin_hash != "9876"
    assert updated.pin_hash != old_hash
    assert updated.pin_assumed_from_iiko is False
    assert updated.pin_set_at is not None
    assert actions[-1].action_type == "change_pin"
    assert actions[-1].after_value["pin_changed"] is True
    assert events[-1].change_type == "change_pin"
    assert events[-1].diff == {"pin_changed": True}
    assert "pin_hash" not in str(events[-1].before_value)
    assert "pin_hash" not in str(events[-1].after_value)
    assert "9876" not in str(events[-1].diff)


def test_employee_pin_change_request_rejects_non_four_digit_pin() -> None:
    with pytest.raises(ValueError):
        EmployeePinChangeRequest(pin_code="123")
    with pytest.raises(ValueError):
        EmployeePinChangeRequest(pin_code="12345")


async def test_set_hire_date_for_null_succeeds() -> None:
    employee = make_employee(iiko_id="iiko-hire-date-null", full_name="Hire Date Null")
    session = FakeSession([employee])
    hire_date = years_ago(date.today(), 1)

    updated = await set_employee_hire_date(
        employee.id,
        EmployeeHireDateRequest(hire_date=hire_date, comment="  дата из анкеты  "),
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    assert updated.hire_date == hire_date
    assert updated.tenure_started_at == hire_date
    assert session.committed is True


async def test_set_hire_date_current_employee_can_be_updated() -> None:
    employee = make_employee(iiko_id="iiko-hire-date-update", full_name="Hire Date Update")
    session = FakeSession([employee])
    actor = CurrentActor(roles=frozenset({"finance_manager"}))
    first_hire_date = years_ago(date.today(), 1)
    updated_hire_date = years_ago(date.today(), 2)

    await set_employee_hire_date(
        employee.id,
        EmployeeHireDateRequest(hire_date=first_hire_date),
        session,  # type: ignore[arg-type]
        actor,
    )
    updated = await set_employee_hire_date(
        employee.id,
        EmployeeHireDateRequest(hire_date=updated_hire_date),
        session,  # type: ignore[arg-type]
        actor,
    )

    assert updated.hire_date == updated_hire_date
    assert updated.tenure_started_at == updated_hire_date
    events = [item for item in session.added if isinstance(item, EmployeeChangeEvent)]
    assert events[-1].summary == f"Изменена дата приёма {updated_hire_date.strftime('%d.%m.%Y')}"
    assert events[-1].before_value == {
        "hire_date": first_hire_date.isoformat(),
        "tenure_started_at": first_hire_date.isoformat(),
    }


async def test_set_hire_date_inactive_employee_fails_with_409() -> None:
    employee = make_employee(
        iiko_id="iiko-hire-date-inactive",
        full_name="Hire Date Inactive",
        status="inactive",
        fire_date=date.today(),
    )
    session = FakeSession([employee])

    with pytest.raises(HTTPException) as exc_info:
        await set_employee_hire_date(
            employee.id,
            EmployeeHireDateRequest(hire_date=years_ago(date.today(), 1)),
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"finance_manager"})),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Дата приёма недоступна для уволенного сотрудника"


async def test_set_hire_date_in_future_fails_with_422() -> None:
    employee = make_employee(iiko_id="iiko-hire-date-future", full_name="Hire Date Future")
    session = FakeSession([employee])

    with pytest.raises(HTTPException) as exc_info:
        await set_employee_hire_date(
            employee.id,
            EmployeeHireDateRequest(hire_date=date.today() + timedelta(days=1)),
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"finance_manager"})),
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "Дата приёма не может быть в будущем"
    assert employee.hire_date is None


async def test_set_hire_date_too_old_fails_with_422() -> None:
    employee = make_employee(iiko_id="iiko-hire-date-too-old", full_name="Hire Date Too Old")
    session = FakeSession([employee])

    with pytest.raises(HTTPException) as exc_info:
        await set_employee_hire_date(
            employee.id,
            EmployeeHireDateRequest(hire_date=years_ago(date.today(), 11)),
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"finance_manager"})),
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "Дата приёма не может быть раньше чем 10 лет назад"
    assert employee.hire_date is None


async def test_set_hire_date_creates_change_event() -> None:
    employee = make_employee(iiko_id="iiko-hire-date-event", full_name="Hire Date Event")
    session = FakeSession([employee])
    hire_date = years_ago(date.today(), 1)

    await set_employee_hire_date(
        employee.id,
        EmployeeHireDateRequest(hire_date=hire_date, comment="Анкета"),
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    events = [item for item in session.added if isinstance(item, EmployeeChangeEvent)]
    assert len(events) == 1
    assert events[0].change_type == "set_hire_date"
    assert events[0].source == "app"
    assert events[0].effective_from == hire_date
    assert events[0].summary == f"Установлена дата приёма {hire_date.strftime('%d.%m.%Y')}"
    assert events[0].before_value == {"hire_date": None, "tenure_started_at": None}
    assert events[0].after_value == {
        "hire_date": hire_date.isoformat(),
        "tenure_started_at": hire_date.isoformat(),
    }
    assert events[0].comment == "Анкета"
    assert events[0].payroll_impact is True


async def test_set_hire_date_recalculates_fund_tenure() -> None:
    employee = make_employee(iiko_id="iiko-hire-date-fund", full_name="Hire Date Fund")
    session = FakeSession([employee])
    today = date.today()
    hire_date = years_ago(today, 1)

    await set_employee_hire_date(
        employee.id,
        EmployeeHireDateRequest(hire_date=hire_date),
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    settings = {
        "payroll.fund_rates_by_tenure": [
            {"min_months": 0, "rate": "0"},
            {"min_months": 12, "rate": "0.05"},
        ]
    }
    assert tenure_months_on(employee, today) == 12
    assert fund_accrual_for_day(settings, employee, today, Decimal("1000")) == Decimal("50")


async def test_patch_employee_hire_date_rejected_when_set() -> None:
    employee = make_employee(iiko_id="iiko-hire-date-patch", full_name="Hire Date Patch")
    employee.hire_date = years_ago(date.today(), 1)
    employee.tenure_started_at = employee.hire_date
    session = FakeSession([employee])

    with pytest.raises(HTTPException) as exc_info:
        await patch_employee(
            employee.id,
            {"hire_date": years_ago(date.today(), 2)},
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"finance_manager"})),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Дата приёма уже установлена и не подлежит изменению"
    assert employee.hire_date == years_ago(date.today(), 1)


async def test_patch_employee_pin_code_sets_hash_timestamp_and_audit() -> None:
    employee = make_employee(
        iiko_id="iiko-patch-pin",
        full_name="Александр Ушанов",
        status="requires_setup",
        pin_hash=None,
        pin_assumed_from_iiko=True,
    )
    employee.role_assignments.append(make_assignment(employee))
    session = FakeSession([employee])

    updated = await patch_employee(
        employee.id,
        {"pin_code": "1234"},
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    actions = [item for item in session.added if isinstance(item, AgentAction)]
    serialized = EmployeeRead.model_validate(updated).model_dump()
    assert updated.pin_hash is not None
    assert updated.pin_hash != "1234"
    assert verify_password("1234", updated.pin_hash)
    assert updated.pin_assumed_from_iiko is False
    assert updated.pin_set_at is not None
    assert updated.status == "active"
    assert "pin_hash" not in serialized
    assert serialized["pin_assumed_from_iiko"] is False
    pin_action = next(item for item in actions if item.action_type == "pin_set")
    assert pin_action.after_value == {"pin_set_at": updated.pin_set_at.isoformat()}
    assert "pin_hash" not in str(pin_action.after_value)


async def test_patch_employee_pin_code_rejects_non_four_digit_pin() -> None:
    employee = make_employee(iiko_id="iiko-patch-pin-invalid", full_name="Invalid Pin")
    session = FakeSession([employee])

    with pytest.raises(HTTPException) as exc_info:
        await patch_employee(
            employee.id,
            {"pin_code": "12"},
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"finance_manager"})),
        )

    assert exc_info.value.status_code == 422


async def test_patch_employee_pin_code_null_does_not_change_pin() -> None:
    employee = make_employee(iiko_id="iiko-patch-pin-null", full_name="Pin Null")
    old_hash = employee.pin_hash
    old_pin_set_at = employee.pin_set_at
    session = FakeSession([employee])

    updated = await patch_employee(
        employee.id,
        {"pin_code": None, "category": "category_3"},
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    assert updated.pin_hash == old_hash
    assert updated.pin_set_at == old_pin_set_at


async def test_patch_employee_same_pin_rehashes_updates_timestamp_and_audits() -> None:
    employee = make_employee(iiko_id="iiko-patch-pin-repeat", full_name="Pin Repeat")
    session = FakeSession([employee])

    first = await patch_employee(
        employee.id,
        {"pin_code": "2468"},
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )
    first_hash = first.pin_hash
    first_pin_set_at = first.pin_set_at

    second = await patch_employee(
        employee.id,
        {"pin_code": "2468"},
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    pin_actions = [
        item
        for item in session.added
        if isinstance(item, AgentAction) and item.action_type == "pin_changed"
    ]
    assert second.pin_hash is not None
    assert first_hash is not None
    assert second.pin_hash != first_hash
    assert second.pin_set_at is not None
    assert first_pin_set_at is not None
    assert second.pin_set_at != first_pin_set_at
    assert len(pin_actions) == 2


def test_compute_status_active_when_pin_and_canonical_fields_are_set() -> None:
    employee = make_employee(
        iiko_id="iiko-alexander-ushanov",
        full_name="Александр Ушанов",
        status="requires_setup",
        pin_hash="hashed-pin",
    )
    assignment = make_assignment(employee)

    assert (
        compute_status(
            employee,
            is_iiko_deleted=False,
            position_group=position_group_for_position(employee.position),
            assignments=[assignment],
        )
        == "active"
    )


def test_compute_status_active_when_iiko_pin_is_assumed_without_local_pin() -> None:
    """Канон: сотрудник, импортированный из iiko, считается имеющим ПИН в iiko.

    pin_assumed_from_iiko=True — достаточно, чтобы не блокировать статус по ПИН.
    Локальный pin_hash не обязателен для перехода в active, если остальные канон-поля
    (ФИО, должность, роль/категория) заполнены.
    """
    employee = make_employee(
        iiko_id="iiko-assumed-pin",
        full_name="Игорь Синхронов",
        status="requires_setup",
        pin_hash=None,
        pin_assumed_from_iiko=True,
    )
    assignment = make_assignment(employee)

    assert (
        compute_status(
            employee,
            is_iiko_deleted=False,
            position_group=position_group_for_position(employee.position),
            assignments=[assignment],
        )
        == "active"
    )


@pytest.mark.parametrize("position", ["Уборщица", "Посудомойка"])
def test_compute_status_auxiliary_position_active_without_pin(position: str) -> None:
    employee = make_employee(
        iiko_id=f"iiko-{position}",
        full_name=f"{position} Тест",
        status="requires_setup",
        position=position,
        category=None,
        default_cooking_station=None,
        pin_hash=None,
    )

    assert (
        compute_status(
            employee,
            is_iiko_deleted=False,
            position_group=position_group_for_position(employee.position),
            assignments=[],
        )
        == "active"
    )


@pytest.mark.parametrize("position", ["Кассир", "Повар"])
def test_compute_status_role_position_without_pin_requires_setup(position: str) -> None:
    employee = make_employee(
        iiko_id=f"iiko-no-pin-{position}",
        full_name=f"{position} Без Пина",
        position=position,
        category=None,
        default_cooking_station=None,
        pin_hash=None,
    )

    assert (
        compute_status(
            employee,
            is_iiko_deleted=False,
            position_group=position_group_for_position(employee.position),
            assignments=[],
        )
        == "requires_setup"
    )


async def test_create_employee_creates_iiko_first_then_local_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession([])
    calls: list[tuple[str, str, str]] = []

    async def fake_get_iiko_roles(_session: Any) -> list[IikoEmployeeRole]:
        return [IikoEmployeeRole(id="role-cook", name="Повар", code="CO1")]

    async def fake_create_iiko(
        _session: Any,
        *,
        full_name: str,
        role_id: str,
        pin_code: str,
    ) -> IikoEmployeeCreateResult:
        calls.append((full_name, role_id, pin_code))
        return IikoEmployeeCreateResult(
            iiko_id="iiko-new",
            full_name=full_name,
            position="Повар",
            role_id=role_id,
            role_code="CO1",
            is_target_position=True,
        )

    monkeypatch.setattr(employee_routes, "get_iiko_employee_roles", fake_get_iiko_roles)
    monkeypatch.setattr(employee_routes, "create_iiko_employee_in_iiko", fake_create_iiko)

    employee = await create_employee(
        EmployeeCreateRequest(
            full_name=" Новый Сотрудник ",
            pin_code="1234",
            iiko_role_id="role-cook",
            roles=[
                {
                    "payroll_role": "sushi",
                    "category": "category_1",
                    "is_primary": True,
                }
            ],
            is_senior=True,
        ),
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    actions = [item for item in session.added if isinstance(item, AgentAction)]
    events = [item for item in session.added if isinstance(item, EmployeeChangeEvent)]
    assignments = [item for item in session.added if isinstance(item, EmployeeRoleAssignment)]
    assert calls == [("Новый Сотрудник", "role-cook", "1234")]
    assert employee.iiko_id == "iiko-new"
    assert employee.full_name == "Новый Сотрудник"
    assert employee.position == "Повар"
    assert employee.category == "category_1"
    assert employee.default_cooking_station == "sushi"
    assert employee.is_senior is True
    assert employee.hire_date == date.today()
    assert employee.status == "active"
    assert len(assignments) == 1
    assert assignments[0].payroll_role == "sushi"
    assert assignments[0].is_primary is True
    assert session.committed is True
    assert len(actions) == 1
    assert actions[0].action_type == "create"
    assert actions[0].before_value is None
    assert actions[0].after_value["iiko_role_id"] == "role-cook"
    assert actions[0].after_value["roles"][0]["payroll_role"] == "sushi"
    assert len(events) == 1
    assert events[0].change_type == "create_employee"
    assert events[0].source == "app"
    assert events[0].employee_id == employee.id
    assert events[0].after_value["roles"][0]["payroll_role"] == "sushi"
    assert employee.pin_hash is not None
    assert employee.pin_hash != "1234"
    assert employee.pin_assumed_from_iiko is False
    assert employee.pin_set_at is not None


async def test_create_cook_allows_multiple_roles_with_one_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession([])

    async def fake_get_iiko_roles(_session: Any) -> list[IikoEmployeeRole]:
        return [IikoEmployeeRole(id="role-cook", name="Повар", code="CO1")]

    async def fake_create_iiko(
        _session: Any,
        *,
        full_name: str,
        role_id: str,
        pin_code: str,
    ) -> IikoEmployeeCreateResult:
        return IikoEmployeeCreateResult(
            iiko_id="iiko-new-multi-cook",
            full_name=full_name,
            position="Повар",
            role_id=role_id,
            role_code="CO1",
            is_target_position=True,
        )

    monkeypatch.setattr(employee_routes, "get_iiko_employee_roles", fake_get_iiko_roles)
    monkeypatch.setattr(employee_routes, "create_iiko_employee_in_iiko", fake_create_iiko)

    employee = await create_employee(
        EmployeeCreateRequest(
            full_name="Новый Мультиповар",
            pin_code="1234",
            iiko_role_id="role-cook",
            roles=[
                {
                    "payroll_role": "pizza",
                    "category": "category_2",
                    "is_primary": False,
                },
                {
                    "payroll_role": "sushi",
                    "category": "category_1",
                    "is_primary": True,
                },
            ],
        ),
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    assignments = [item for item in session.added if isinstance(item, EmployeeRoleAssignment)]
    by_role = {assignment.payroll_role: assignment for assignment in assignments}
    assert employee.status == "active"
    assert employee.category == "category_1"
    assert employee.default_cooking_station == "sushi"
    assert set(by_role) == {"pizza", "sushi"}
    assert by_role["sushi"].is_primary is True
    assert by_role["pizza"].category == "category_2"
    assert by_role["pizza"].is_primary is False


async def test_create_cashier_creates_administrator_assignment_automatically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession([])

    async def fake_get_iiko_roles(_session: Any) -> list[IikoEmployeeRole]:
        return [IikoEmployeeRole(id="role-cashier", name="Кассир", code="CA")]

    async def fake_create_iiko(
        _session: Any,
        *,
        full_name: str,
        role_id: str,
        pin_code: str,
    ) -> IikoEmployeeCreateResult:
        return IikoEmployeeCreateResult(
            iiko_id="iiko-cashier",
            full_name=full_name,
            position="Кассир",
            role_id=role_id,
            role_code="CA",
            is_target_position=True,
        )

    monkeypatch.setattr(employee_routes, "get_iiko_employee_roles", fake_get_iiko_roles)
    monkeypatch.setattr(employee_routes, "create_iiko_employee_in_iiko", fake_create_iiko)

    employee = await create_employee(
        EmployeeCreateRequest(
            full_name="Новый Кассир",
            pin_code="2468",
            iiko_role_id="role-cashier",
            category="category_2",
        ),
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    assignments = [item for item in session.added if isinstance(item, EmployeeRoleAssignment)]
    assert employee.position == "Кассир"
    assert employee.category == "category_2"
    assert employee.default_cooking_station is None
    assert employee.status == "active"
    assert len(assignments) == 1
    assert assignments[0].payroll_role == "administrator"
    assert assignments[0].category == "category_2"
    assert assignments[0].is_primary is True


async def test_create_employee_rejects_invalid_role_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession([])

    async def fake_get_iiko_roles(_session: Any) -> list[IikoEmployeeRole]:
        return [IikoEmployeeRole(id="role-cook", name="Повар", code="CO1")]

    async def fake_create_iiko(
        _session: Any,
        *,
        full_name: str,
        role_id: str,
        pin_code: str,
    ) -> IikoEmployeeCreateResult:
        raise AssertionError("iiko create must not be called for invalid taxonomy")

    monkeypatch.setattr(employee_routes, "get_iiko_employee_roles", fake_get_iiko_roles)
    monkeypatch.setattr(employee_routes, "create_iiko_employee_in_iiko", fake_create_iiko)

    with pytest.raises(HTTPException) as exc_info:
        await create_employee(
            EmployeeCreateRequest(
                full_name="Новый Сотрудник",
                pin_code="1234",
                iiko_role_id="role-cook",
                roles=[
                    {
                        "payroll_role": "shawarma",
                        "category": "category_1",
                        "is_primary": True,
                    }
                ],
            ),
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"finance_manager"})),
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "Категория недоступна для этой роли"
    assert session.employees == []


async def test_create_employee_allows_courier_without_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession([])

    async def fake_get_iiko_roles(_session: Any) -> list[IikoEmployeeRole]:
        return [IikoEmployeeRole(id="role-courier", name="Курьер", code="CR")]

    async def fake_create_iiko(
        _session: Any,
        *,
        full_name: str,
        role_id: str,
        pin_code: str,
    ) -> IikoEmployeeCreateResult:
        return IikoEmployeeCreateResult(
            iiko_id="iiko-courier",
            full_name=full_name,
            position="Курьер",
            role_id=role_id,
            role_code="CR",
            is_target_position=True,
        )

    monkeypatch.setattr(employee_routes, "get_iiko_employee_roles", fake_get_iiko_roles)
    monkeypatch.setattr(employee_routes, "create_iiko_employee_in_iiko", fake_create_iiko)

    employee = await create_employee(
        EmployeeCreateRequest(
            full_name="Новый Курьер",
            pin_code="4321",
            iiko_role_id="role-courier",
            roles=[],
            is_senior=True,
        ),
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    assert employee.position == "Курьер"
    assert employee.category is None
    assert employee.default_cooking_station is None
    assert employee.is_senior is True
    assert employee.is_deputy_senior is False
    assert employee.status == "active"
    assert [item for item in session.added if isinstance(item, EmployeeRoleAssignment)] == []


@pytest.mark.parametrize("position", ["Курьер", "Менеджер", "Управляющий"])
async def test_create_employee_allows_roleless_positions_without_roles(
    monkeypatch: pytest.MonkeyPatch,
    position: str,
) -> None:
    session = FakeSession([])
    role_id = f"role-{position}"

    async def fake_get_iiko_roles(_session: Any) -> list[IikoEmployeeRole]:
        return [IikoEmployeeRole(id=role_id, name=position, code=None)]

    async def fake_create_iiko(
        _session: Any,
        *,
        full_name: str,
        role_id: str,
        pin_code: str,
    ) -> IikoEmployeeCreateResult:
        return IikoEmployeeCreateResult(
            iiko_id=f"iiko-{position}",
            full_name=full_name,
            position=position,
            role_id=role_id,
            role_code=None,
            is_target_position=True,
        )

    monkeypatch.setattr(employee_routes, "get_iiko_employee_roles", fake_get_iiko_roles)
    monkeypatch.setattr(employee_routes, "create_iiko_employee_in_iiko", fake_create_iiko)

    employee = await create_employee(
        EmployeeCreateRequest(
            full_name=f"Новый {position}",
            pin_code="4321",
            iiko_role_id=role_id,
            roles=[],
        ),
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    assert employee.position == position
    assert employee.category is None
    assert employee.default_cooking_station is None
    assert employee.status == "active"
    assert [item for item in session.added if isinstance(item, EmployeeRoleAssignment)] == []


async def test_create_employee_rejects_position_outside_canonical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession([])

    async def fake_get_iiko_roles(_session: Any) -> list[IikoEmployeeRole]:
        return [IikoEmployeeRole(id="role-waiter", name="Официант", code="WA")]

    monkeypatch.setattr(employee_routes, "get_iiko_employee_roles", fake_get_iiko_roles)

    with pytest.raises(HTTPException) as exc_info:
        await create_employee(
            EmployeeCreateRequest(
                full_name="Новый Официант",
                pin_code="1234",
                iiko_role_id="role-waiter",
                roles=[],
            ),
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"finance_manager"})),
        )

    assert exc_info.value.status_code == 422
    assert session.employees == []


async def test_create_employee_iiko_error_does_not_create_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession([])

    async def fake_get_iiko_roles(_session: Any) -> list[IikoEmployeeRole]:
        return [IikoEmployeeRole(id="role-cook", name="Повар", code="CO1")]

    async def fake_create_iiko(
        _session: Any,
        *,
        full_name: str,
        role_id: str,
        pin_code: str,
    ) -> IikoEmployeeCreateResult:
        raise IikoEmployeeOperationError("iiko не создал сотрудника")

    monkeypatch.setattr(employee_routes, "get_iiko_employee_roles", fake_get_iiko_roles)
    monkeypatch.setattr(employee_routes, "create_iiko_employee_in_iiko", fake_create_iiko)

    with pytest.raises(HTTPException) as exc_info:
        await create_employee(
            EmployeeCreateRequest(
                full_name="Новый Сотрудник",
                pin_code="1234",
                iiko_role_id="role-cook",
                roles=[
                    {
                        "payroll_role": "sushi",
                        "category": "category_1",
                        "is_primary": True,
                    }
                ],
            ),
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"finance_manager"})),
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "iiko не создал сотрудника"
    assert session.employees == []
    assert session.committed is False


async def test_second_senior_cook_is_rejected_with_existing_cook_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = make_employee(iiko_id="iiko-senior-cook", full_name="Старший Повар")
    existing.is_senior = True
    session = FakeSession([existing])

    async def fake_get_iiko_roles(_session: Any) -> list[IikoEmployeeRole]:
        return [IikoEmployeeRole(id="role-cook", name="Повар", code="CO")]

    async def fake_create_iiko(
        _session: Any,
        *,
        full_name: str,
        role_id: str,
        pin_code: str,
    ) -> IikoEmployeeCreateResult:
        raise AssertionError("iiko create must not be called when senior limit is exceeded")

    monkeypatch.setattr(employee_routes, "get_iiko_employee_roles", fake_get_iiko_roles)
    monkeypatch.setattr(employee_routes, "create_iiko_employee_in_iiko", fake_create_iiko)

    with pytest.raises(HTTPException) as exc_info:
        await create_employee(
            EmployeeCreateRequest(
                full_name="Второй Старший",
                pin_code="1111",
                iiko_role_id="role-cook",
                roles=[
                    {
                        "payroll_role": "sushi",
                        "category": "category_1",
                        "is_primary": True,
                    }
                ],
                is_senior=True,
            ),
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"finance_manager"})),
        )

    assert exc_info.value.status_code == 422
    assert "Старший Повар" in str(exc_info.value.detail)


async def test_second_deputy_senior_cook_is_rejected_with_existing_cook_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = make_employee(iiko_id="iiko-deputy-cook", full_name="Зам Повар")
    existing.is_deputy_senior = True
    session = FakeSession([existing])

    async def fake_get_iiko_roles(_session: Any) -> list[IikoEmployeeRole]:
        return [IikoEmployeeRole(id="role-cook", name="Повар", code="CO")]

    async def fake_create_iiko(
        _session: Any,
        *,
        full_name: str,
        role_id: str,
        pin_code: str,
    ) -> IikoEmployeeCreateResult:
        raise AssertionError("iiko create must not be called when deputy limit is exceeded")

    monkeypatch.setattr(employee_routes, "get_iiko_employee_roles", fake_get_iiko_roles)
    monkeypatch.setattr(employee_routes, "create_iiko_employee_in_iiko", fake_create_iiko)

    with pytest.raises(HTTPException) as exc_info:
        await create_employee(
            EmployeeCreateRequest(
                full_name="Второй Зам",
                pin_code="1111",
                iiko_role_id="role-cook",
                roles=[
                    {
                        "payroll_role": "sushi",
                        "category": "category_1",
                        "is_primary": True,
                    }
                ],
                is_deputy_senior=True,
            ),
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"finance_manager"})),
        )

    assert exc_info.value.status_code == 422
    assert "Зам Повар" in str(exc_info.value.detail)


async def test_second_senior_cashier_is_rejected_with_existing_admin_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = make_employee(
        iiko_id="iiko-senior-cashier-1",
        full_name="Первый Администратор",
        position="Кассир",
        category="category_2",
        default_cooking_station=None,
    )
    first.is_senior = True
    session = FakeSession([first])

    async def fake_get_iiko_roles(_session: Any) -> list[IikoEmployeeRole]:
        return [IikoEmployeeRole(id="role-cashier", name="Кассир", code="CA")]

    async def fake_create_iiko(
        _session: Any,
        *,
        full_name: str,
        role_id: str,
        pin_code: str,
    ) -> IikoEmployeeCreateResult:
        raise AssertionError("iiko create must not be called when cashier limit is exceeded")

    monkeypatch.setattr(employee_routes, "get_iiko_employee_roles", fake_get_iiko_roles)
    monkeypatch.setattr(employee_routes, "create_iiko_employee_in_iiko", fake_create_iiko)

    with pytest.raises(HTTPException) as exc_info:
        await create_employee(
            EmployeeCreateRequest(
                full_name="Третий Старший",
                pin_code="2222",
                iiko_role_id="role-cashier",
                category="category_2",
                is_senior=True,
            ),
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"finance_manager"})),
        )

    detail = str(exc_info.value.detail)
    assert exc_info.value.status_code == 422
    assert "Первый Администратор" in detail


async def test_assign_second_senior_fails_409_with_existing() -> None:
    existing = make_employee(iiko_id="iiko-existing-senior", full_name="Иванов Старший")
    existing.is_senior = True
    target = make_employee(iiko_id="iiko-target-senior", full_name="Петров Новый")
    session = FakeSession([existing, target])

    with pytest.raises(HTTPException) as exc_info:
        await patch_employee(
            target.id,
            {"is_senior": True},
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"finance_manager"})),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "senior_already_assigned"
    assert exc_info.value.detail["existing_employee_id"] == str(existing.id)


async def test_assign_second_senior_with_transfer_succeeds() -> None:
    existing = make_employee(iiko_id="iiko-transfer-from", full_name="Иванов Старший")
    existing.is_senior = True
    target = make_employee(iiko_id="iiko-transfer-to", full_name="Петров Новый")
    session = FakeSession([existing, target])

    updated = await patch_employee(
        target.id,
        {"is_senior": True, "transfer_from_existing": True},
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    assert existing.is_senior is False
    assert updated.is_senior is True
    assert session.committed is True


async def test_patch_employee_iiko_deleted_stays_inactive() -> None:
    employee = make_employee(
        iiko_id="iiko-15",
        full_name="Deleted Cook",
        status="inactive",
        position="Повар",
        category=None,
        default_cooking_station="sushi",
    )
    session = FakeSession([employee])

    updated = await patch_employee(
        employee.id,
        {"category": "category_1"},
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    assert updated.category == "category_1"
    assert updated.status == "inactive"


def test_compute_status_fire_date_overrides_active_setup() -> None:
    employee = make_employee(
        iiko_id="iiko-16",
        full_name="Fired Cook",
        status="active",
        fire_date=SYNC_TODAY,
    )

    status = compute_status(
        employee,
        is_iiko_deleted=False,
        position_group=position_group_for_position(employee.position),
    )

    assert status == "inactive"


async def test_dismiss_employee_sets_status_fire_date_reason_and_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    employee = make_employee(iiko_id="iiko-17", full_name="Dismiss Me")
    session = FakeSession([employee])
    iiko_calls: list[str] = []

    async def fake_dismiss_iiko(_session: Any, *, iiko_id: str) -> None:
        iiko_calls.append(iiko_id)

    monkeypatch.setattr(employee_routes, "dismiss_iiko_employee_in_iiko", fake_dismiss_iiko)

    updated = await dismiss_employee(
        employee.id,
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
        EmployeeDismissRequest(fire_date=SYNC_TODAY, reason="  Переезд  "),
    )

    actions = [item for item in session.added if isinstance(item, AgentAction)]
    events = [item for item in session.added if isinstance(item, EmployeeChangeEvent)]
    assert updated.status == "inactive"
    assert updated.fire_date == SYNC_TODAY
    assert updated.fire_reason == "Переезд"
    assert session.committed is True
    assert len(actions) == 1
    assert actions[0].before_value["status"] == "active"
    assert actions[0].after_value["status"] == "inactive"
    assert actions[0].after_value["fire_reason"] == "Переезд"
    assert len(events) == 1
    assert events[0].change_type == "dismiss"
    assert events[0].reason_label == "Переезд"
    assert events[0].comment == "Переезд"
    assert events[0].payroll_impact is True
    assert iiko_calls == ["iiko-17"]


async def test_dismiss_employee_twice_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    employee = make_employee(iiko_id="iiko-18", full_name="Already Dismissed")
    session = FakeSession([employee])
    actor = CurrentActor(roles=frozenset({"finance_manager"}))

    async def fake_dismiss_iiko(_session: Any, *, iiko_id: str) -> None:
        assert iiko_id == "iiko-18"

    monkeypatch.setattr(employee_routes, "dismiss_iiko_employee_in_iiko", fake_dismiss_iiko)

    await dismiss_employee(
        employee.id,
        session,  # type: ignore[arg-type]
        actor,
        EmployeeDismissRequest(
            fire_date=SYNC_TODAY,
            reason_code="transfer",
            comment="Перенос задним числом",
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await dismiss_employee(
            employee.id,
            session,  # type: ignore[arg-type]
            actor,
            EmployeeDismissRequest(
                fire_date=SYNC_TODAY,
                reason_code="transfer",
                comment="Повтор",
            ),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Сотрудник уже уволен"


async def test_dismiss_employee_without_role_returns_403() -> None:
    employee = make_employee(iiko_id="iiko-19", full_name="No Role")
    session = FakeSession([employee])

    with pytest.raises(HTTPException) as exc_info:
        await dismiss_employee(
            employee.id,
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset()),
            EmployeeDismissRequest(fire_date=SYNC_TODAY),
        )

    assert exc_info.value.status_code == 403
    assert employee.status == "active"


async def test_dismiss_employee_manager_returns_403() -> None:
    employee = make_employee(iiko_id="iiko-20", full_name="Manager Denied")
    session = FakeSession([employee])

    with pytest.raises(HTTPException) as exc_info:
        await dismiss_employee(
            employee.id,
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"manager"})),
            EmployeeDismissRequest(fire_date=SYNC_TODAY),
        )

    assert exc_info.value.status_code == 403
    assert employee.status == "active"


async def test_dismiss_employee_requires_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    employee = make_employee(iiko_id="iiko-missing-reason", full_name="Missing Reason")
    session = FakeSession([employee])

    async def fake_dismiss_iiko(_session: Any, *, iiko_id: str) -> None:
        raise AssertionError("iiko must not be called when reason is missing")

    monkeypatch.setattr(employee_routes, "dismiss_iiko_employee_in_iiko", fake_dismiss_iiko)

    with pytest.raises(HTTPException) as exc_info:
        await dismiss_employee(
            employee.id,
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"finance_manager"})),
            EmployeeDismissRequest(fire_date=date.today()),
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "Причина увольнения обязательна"
    assert employee.status == "active"


async def test_dismiss_employee_other_reason_requires_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    employee = make_employee(iiko_id="iiko-other-reason", full_name="Other Reason")
    session = FakeSession([employee])

    async def fake_dismiss_iiko(_session: Any, *, iiko_id: str) -> None:
        raise AssertionError("iiko must not be called when other comment is missing")

    monkeypatch.setattr(employee_routes, "dismiss_iiko_employee_in_iiko", fake_dismiss_iiko)

    with pytest.raises(HTTPException) as exc_info:
        await dismiss_employee(
            employee.id,
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"finance_manager"})),
            EmployeeDismissRequest(fire_date=date.today(), reason_code="other"),
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "Для причины «Другое» комментарий обязателен"
    assert employee.status == "active"


async def test_dismiss_employee_regular_reason_without_comment_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    employee = make_employee(iiko_id="iiko-regular-reason", full_name="Regular Reason")
    session = FakeSession([employee])

    async def fake_dismiss_iiko(_session: Any, *, iiko_id: str) -> None:
        assert iiko_id == "iiko-regular-reason"

    monkeypatch.setattr(employee_routes, "dismiss_iiko_employee_in_iiko", fake_dismiss_iiko)

    updated = await dismiss_employee(
        employee.id,
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
        EmployeeDismissRequest(fire_date=date.today(), reason_code="transfer"),
    )

    events = [item for item in session.added if isinstance(item, EmployeeChangeEvent)]
    assert updated.status == "inactive"
    assert updated.fire_reason == "Перевод"
    assert events[0].reason_code == "transfer"
    assert events[0].comment is None


async def test_dismiss_employee_finance_manager_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    employee = make_employee(iiko_id="iiko-21", full_name="Finance Allowed")
    session = FakeSession([employee])

    async def fake_dismiss_iiko(_session: Any, *, iiko_id: str) -> None:
        assert iiko_id == "iiko-21"

    monkeypatch.setattr(employee_routes, "dismiss_iiko_employee_in_iiko", fake_dismiss_iiko)

    updated = await dismiss_employee(
        employee.id,
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
        EmployeeDismissRequest(
            fire_date=SYNC_TODAY,
            reason_code="transfer",
            comment="Перенос задним числом",
        ),
    )

    assert updated.status == "inactive"
    assert updated.fire_date == SYNC_TODAY


async def test_dismiss_employee_iiko_error_keeps_local_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    employee = make_employee(iiko_id="iiko-err", full_name="Still Active")
    session = FakeSession([employee])

    async def fake_dismiss_iiko(_session: Any, *, iiko_id: str) -> None:
        assert iiko_id == "iiko-err"
        raise IikoEmployeeOperationError("iiko отклонил увольнение")

    monkeypatch.setattr(employee_routes, "dismiss_iiko_employee_in_iiko", fake_dismiss_iiko)

    with pytest.raises(HTTPException) as exc_info:
        await dismiss_employee(
            employee.id,
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"finance_manager"})),
            EmployeeDismissRequest(
                fire_date=SYNC_TODAY,
                reason_code="transfer",
                comment="Перенос задним числом",
            ),
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "iiko отклонил увольнение"
    assert employee.status == "active"
    assert employee.fire_date is None
    assert session.committed is False


async def test_dismiss_employee_closes_open_shift(monkeypatch: pytest.MonkeyPatch) -> None:
    employee = make_employee(iiko_id="iiko-shift", full_name="Open Shift")
    entry = ShiftLedgerEntry(
        id=uuid.uuid4(),
        work_date=SYNC_TODAY,
        employee_id=employee.id,
        payroll_role="sushi",
        category="category_2",
        source="manual_correction",
        opened_at=SYNC_NOW,
        closed_at=None,
        notes=None,
        is_resolved=True,
    )
    session = FakeSession([employee], shift_entries=[entry])

    async def fake_dismiss_iiko(_session: Any, *, iiko_id: str) -> None:
        assert iiko_id == "iiko-shift"

    monkeypatch.setattr(employee_routes, "dismiss_iiko_employee_in_iiko", fake_dismiss_iiko)

    await dismiss_employee(
        employee.id,
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
        EmployeeDismissRequest(
            fire_date=SYNC_TODAY,
            reason_code="transfer",
            comment="Перенос задним числом",
        ),
    )

    shift_actions = [
        item
        for item in session.added
        if isinstance(item, AgentAction) and item.target_table == "shift_ledger_entry"
    ]
    assert entry.closed_at is not None
    assert entry.notes == "Закрыто при увольнении сотрудника"
    assert len(shift_actions) == 1
    assert shift_actions[0].before_value["closed_at"] is None
    assert shift_actions[0].after_value["closed_at"] == entry.closed_at.isoformat()


async def test_dismiss_payout_full_with_notice_14_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    employee = make_employee(iiko_id="iiko-deposit-full", full_name="Deposit Full")
    account = make_deposit_account(employee, Decimal("5000"))
    notice = make_notice_event(employee, SYNC_TODAY - timedelta(days=15))
    session = FakeSession(
        [employee],
        change_events=[notice],
        deposit_accounts=[account],
    )

    async def fake_dismiss_iiko(_session: Any, *, iiko_id: str) -> None:
        assert iiko_id == "iiko-deposit-full"

    monkeypatch.setattr(employee_routes, "dismiss_iiko_employee_in_iiko", fake_dismiss_iiko)

    await dismiss_employee(
        employee.id,
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
        EmployeeDismissRequest(
            fire_date=SYNC_TODAY,
            reason_code="transfer",
            comment="Перенос задним числом",
            deposit_action=DepositDismissAction.PAYOUT_FULL,
        ),
    )

    assert account.balance == Decimal("0")
    assert len(session.deposit_transactions) == 1
    transaction = session.deposit_transactions[0]
    assert transaction.transaction_type == "dismissal_payout"
    assert transaction.amount == Decimal("5000")


async def test_dismiss_payout_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    employee = make_employee(iiko_id="iiko-deposit-partial", full_name="Deposit Partial")
    account = make_deposit_account(employee, Decimal("8000"))
    session = FakeSession([employee], deposit_accounts=[account])

    async def fake_dismiss_iiko(_session: Any, *, iiko_id: str) -> None:
        assert iiko_id == "iiko-deposit-partial"

    monkeypatch.setattr(employee_routes, "dismiss_iiko_employee_in_iiko", fake_dismiss_iiko)

    await dismiss_employee(
        employee.id,
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
        EmployeeDismissRequest(
            fire_date=SYNC_TODAY,
            reason_code="transfer",
            comment="Перенос задним числом",
            deposit_action=DepositDismissAction.PAYOUT_PARTIAL,
            deposit_payout_amount=Decimal("5000"),
        ),
    )

    assert account.balance == Decimal("0")
    assert [(item.transaction_type, item.amount) for item in session.deposit_transactions] == [
        ("dismissal_payout", Decimal("5000")),
        ("dismissal_writeoff", Decimal("3000")),
    ]


async def test_dismiss_writeoff_no_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    employee = make_employee(iiko_id="iiko-deposit-writeoff", full_name="Deposit Writeoff")
    account = make_deposit_account(employee, Decimal("3000"))
    session = FakeSession([employee], deposit_accounts=[account])

    async def fake_dismiss_iiko(_session: Any, *, iiko_id: str) -> None:
        assert iiko_id == "iiko-deposit-writeoff"

    monkeypatch.setattr(employee_routes, "dismiss_iiko_employee_in_iiko", fake_dismiss_iiko)

    await dismiss_employee(
        employee.id,
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
        EmployeeDismissRequest(
            fire_date=SYNC_TODAY,
            reason_code="transfer",
            comment="Перенос задним числом",
            deposit_action=DepositDismissAction.WRITE_OFF,
        ),
    )

    assert account.balance == Decimal("0")
    assert len(session.deposit_transactions) == 1
    assert session.deposit_transactions[0].transaction_type == "dismissal_writeoff"
    assert session.deposit_transactions[0].amount == Decimal("3000")


async def test_dismiss_none_action_with_nonzero_balance_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    employee = make_employee(iiko_id="iiko-deposit-none", full_name="Deposit None")
    account = make_deposit_account(employee, Decimal("1000"))
    session = FakeSession([employee], deposit_accounts=[account])

    async def fake_dismiss_iiko(_session: Any, *, iiko_id: str) -> None:
        raise AssertionError("iiko must not be called when deposit action is invalid")

    monkeypatch.setattr(employee_routes, "dismiss_iiko_employee_in_iiko", fake_dismiss_iiko)

    with pytest.raises(HTTPException) as exc_info:
        await dismiss_employee(
            employee.id,
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"finance_manager"})),
            EmployeeDismissRequest(
                fire_date=SYNC_TODAY,
                reason_code="transfer",
                comment="Перенос задним числом",
                deposit_action=DepositDismissAction.NONE,
            ),
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "Депозит не пуст, выберите действие"
    assert account.balance == Decimal("1000")
    assert session.deposit_transactions == []


async def test_dismiss_partial_amount_exceeds_balance_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    employee = make_employee(iiko_id="iiko-deposit-exceeds", full_name="Deposit Exceeds")
    account = make_deposit_account(employee, Decimal("1000"))
    session = FakeSession([employee], deposit_accounts=[account])

    async def fake_dismiss_iiko(_session: Any, *, iiko_id: str) -> None:
        raise AssertionError("iiko must not be called when deposit amount is invalid")

    monkeypatch.setattr(employee_routes, "dismiss_iiko_employee_in_iiko", fake_dismiss_iiko)

    with pytest.raises(HTTPException) as exc_info:
        await dismiss_employee(
            employee.id,
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"finance_manager"})),
            EmployeeDismissRequest(
                fire_date=SYNC_TODAY,
                reason_code="transfer",
                comment="Перенос задним числом",
                deposit_action=DepositDismissAction.PAYOUT_PARTIAL,
                deposit_payout_amount=Decimal("5000"),
            ),
        )

    assert exc_info.value.status_code == 422
    assert account.balance == Decimal("1000")
    assert session.deposit_transactions == []


async def test_reinstate_does_not_restore_deposit(monkeypatch: pytest.MonkeyPatch) -> None:
    employee = make_employee(iiko_id="iiko-deposit-reinstate", full_name="Deposit Reinstate")
    account = make_deposit_account(employee, Decimal("5000"))
    session = FakeSession([employee], deposit_accounts=[account])

    async def fake_dismiss_iiko(_session: Any, *, iiko_id: str) -> None:
        assert iiko_id == "iiko-deposit-reinstate"

    monkeypatch.setattr(employee_routes, "dismiss_iiko_employee_in_iiko", fake_dismiss_iiko)

    await dismiss_employee(
        employee.id,
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
        EmployeeDismissRequest(
            fire_date=SYNC_TODAY,
            reason_code="transfer",
            comment="Перенос задним числом",
            deposit_action=DepositDismissAction.PAYOUT_FULL,
        ),
    )
    await reinstate_employee(
        employee.id,
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"owner"})),
    )

    assert employee.status == "active"
    assert account.balance == Decimal("0")
    assert len(session.deposit_transactions) == 1
    assert session.deposit_transactions[0].transaction_type == "dismissal_payout"


async def test_notice_lifecycle() -> None:
    employee = make_employee(iiko_id="iiko-notice", full_name="Notice Lifecycle")
    session = FakeSession([employee])
    actor = CurrentActor(roles=frozenset({"finance_manager"}))

    first = await notice_service.record_notice(
        session, employee_id=employee.id, notice_date=SYNC_TODAY, comment=None, actor=actor
    )
    assert await notice_service.get_active_notice(session, employee.id, SYNC_TODAY) == first

    cancelled = await notice_service.cancel_notice(
        session, employee_id=employee.id, comment="Передумал", actor=actor
    )
    assert cancelled.change_type == "notice_cancelled"
    assert await notice_service.get_active_notice(session, employee.id, SYNC_TODAY) is None

    second = await notice_service.record_notice(
        session,
        employee_id=employee.id,
        notice_date=SYNC_TODAY + timedelta(days=1),
        comment="Повторно",
        actor=actor,
    )
    assert second.change_type == "notice_given"

    with pytest.raises(notice_service.NoticeAlreadyExistsError):
        await notice_service.record_notice(
            session,
            employee_id=employee.id,
            notice_date=SYNC_TODAY + timedelta(days=2),
            comment=None,
            actor=actor,
        )


def test_dismiss_employee_endpoint_returns_200(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app()
    employee = make_employee(iiko_id="iiko-22", full_name="Endpoint Dismiss")
    session = FakeSession([employee])

    async def fake_dismiss_iiko(_session: Any, *, iiko_id: str) -> None:
        assert iiko_id == "iiko-22"

    monkeypatch.setattr(employee_routes, "dismiss_iiko_employee_in_iiko", fake_dismiss_iiko)

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/employees/{employee.id}/dismiss",
            headers={"X-User-Role": "finance_manager"},
            json={"fire_date": SYNC_TODAY.isoformat(), "reason": "Сезон завершён"},
        )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == "inactive"
    assert response.json()["fire_date"] == SYNC_TODAY.isoformat()
    assert response.json()["fire_reason"] == "Сезон завершён"


async def test_reinstate_employee_restores_computed_status() -> None:
    employee = make_employee(
        iiko_id="iiko-23",
        full_name="Restore Me",
        status="inactive",
        fire_date=SYNC_TODAY,
        fire_reason="Ошибка",
    )
    session = FakeSession([employee])

    updated = await reinstate_employee(
        employee.id,
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"owner"})),
    )

    assert updated.status == "active"
    assert updated.fire_date is None
    assert updated.fire_reason is None
    assert session.committed is True


async def test_reinstate_employee_finance_manager_returns_403() -> None:
    employee = make_employee(
        iiko_id="iiko-24",
        full_name="Restore Denied",
        status="inactive",
        fire_date=SYNC_TODAY,
    )
    session = FakeSession([employee])

    with pytest.raises(HTTPException) as exc_info:
        await reinstate_employee(
            employee.id,
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"finance_manager"})),
        )

    assert exc_info.value.status_code == 403
    assert employee.status == "inactive"
    assert employee.fire_date == SYNC_TODAY


async def test_get_filter_status_requires_setup_returns_only_unconfigured() -> None:
    session = FakeSession(
        [
            make_employee(
                iiko_id="iiko-7",
                full_name="Needs Setup",
                status="requires_setup",
                position=None,
                category=None,
                default_cooking_station=None,
            ),
            make_employee(iiko_id="iiko-8", full_name="Active Employee", status="active"),
            make_employee(iiko_id="iiko-9", full_name="Inactive Employee", status="inactive"),
        ]
    )

    employees = await list_employees(session, status_filter="requires_setup")  # type: ignore[arg-type]

    assert [employee.iiko_id for employee in employees] == ["iiko-7"]


def test_list_employees_without_trailing_slash_returns_200() -> None:
    app = create_app()

    async def override_session():
        yield FakeSession([])

    app.dependency_overrides[get_session] = override_session

    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/api/v1/employees")

    assert response.status_code == 200
    assert response.json() == []


async def test_employee_sync_endpoint_passes_reset_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, str] = {}

    async def fake_sync_employees(
        _session: Any,
        *,
        run_reason: str,
        mode: str,
    ) -> SyncResult:
        calls["run_reason"] = run_reason
        calls["mode"] = mode
        return SyncResult()

    monkeypatch.setattr(employee_routes, "sync_employees", fake_sync_employees)

    response = await employee_routes.trigger_employee_sync(
        FakeSession([]),  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
        mode="reset",
    )

    assert response == {"created": 0, "updated": 0, "deactivated": 0}
    assert calls == {"run_reason": "manual", "mode": "reset"}


def test_iiko_request_retries_incomplete_read(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[int] = []

    class FlakyClient:
        def __init__(self) -> None:
            self.calls = 0

        def request(self, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
            self.calls += 1
            assert path == "/employees"
            assert kwargs == {"params": {"includeDeleted": "true"}}
            if self.calls < 3:
                raise http.client.IncompleteRead(b"")
            return 200, {"items": []}

    monkeypatch.setattr(iiko_sync_service.time, "sleep", lambda seconds: sleeps.append(seconds))

    client = FlakyClient()
    status_code, data = _request_iiko_with_incomplete_read_retry(
        client,
        "/employees",
        params={"includeDeleted": "true"},
    )

    assert (status_code, data) == (200, {"items": []})
    assert client.calls == 3
    assert sleeps == [5, 5]


def test_build_iiko_employee_update_xml_preserves_unrelated_fields() -> None:
    data = b"""
    <employee>
        <id>iiko-update</id>
        <name>Old Name</name>
        <login>old-login</login>
        <pinCode>1234</pinCode>
        <mainRoleId>old-role</mainRoleId>
        <rolesIds>old-role</rolesIds>
        <mainRoleCode>OLD</mainRoleCode>
        <roleCodes>OLD</roleCodes>
        <hireDate>2026-05-01</hireDate>
    </employee>
    """

    body = _build_iiko_employee_update_xml(
        data,
        full_name="New Name",
        role=IikoEmployeeRole(id="role-cashier", name="Кассир", code="CA"),
    )
    root = ET.fromstring(body)
    values = {child.tag: child.text for child in root}

    assert values["name"] == "New Name"
    assert values["mainRoleId"] == "role-cashier"
    assert values["rolesIds"] == "role-cashier"
    assert values["mainRoleCode"] == "CA"
    assert values["roleCodes"] == "CA"
    assert values["login"] == "old-login"
    assert values["pinCode"] == "1234"
    assert values["hireDate"] == "2026-05-01"


def test_build_iiko_employee_roles_update_xml_sets_main_and_extra_roles() -> None:
    data = b"""
    <employee>
        <id>iiko-update</id>
        <name>Employee Name</name>
        <mainRoleId>old-role</mainRoleId>
        <rolesIds>old-role</rolesIds>
        <mainRoleCode>OLD</mainRoleCode>
        <roleCodes>OLD</roleCodes>
    </employee>
    """
    cashier = IikoEmployeeRole(id="role-cashier", name="Кассир", code="CA")
    cook = IikoEmployeeRole(id="role-cook", name="Повар", code="CO")

    body = _build_iiko_employee_roles_update_xml(
        data,
        main_role=cashier,
        roles=[cashier, cook],
    )
    root = ET.fromstring(body)
    values = {child.tag: child.text for child in root}

    assert values["mainRoleId"] == "role-cashier"
    assert values["rolesIds"] == "role-cashier,role-cook"
    assert values["mainRoleCode"] == "CA"
    assert values["roleCodes"] == "CA,CO"
    assert values["name"] == "Employee Name"


async def test_iiko_extra_role_flags_missing_substitute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    employee = Employee(
        id=uuid.uuid4(),
        iiko_id="iiko-extra-role",
        full_name="Manager With Cook Role",
        position="Управляющий",
        status="active",
        requires_role_review=False,
        role_review_payload=None,
    )

    async def fake_pairs(_session: Any) -> list[Any]:
        return [
            iiko_sync_service.payroll_config.SubstitutePair(
                from_position="Управляющий",
                to_position="Повар",
            )
        ]

    async def fake_covered_positions(_session: Any, _employee_id: uuid.UUID) -> set[str]:
        return set()

    monkeypatch.setattr(iiko_sync_service.payroll_config, "get_substitute_pairs", fake_pairs)
    monkeypatch.setattr(
        iiko_sync_service,
        "_active_substitute_target_positions",
        fake_covered_positions,
    )

    await iiko_sync_service.process_employee_extra_roles(
        object(),  # type: ignore[arg-type]
        employee,
        {"extra_iiko_positions": ["Повар"]},
    )

    assert employee.requires_role_review is True
    assert employee.role_review_payload is not None
    assert employee.role_review_payload["reason"] == "missing_substitute"
    assert employee.role_review_payload["extra_iiko_positions"] == ["Повар"]


async def test_employee_sync_endpoint_returns_bad_gateway_for_incomplete_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_sync_employees(
        _session: Any,
        *,
        run_reason: str,
        mode: str,
    ) -> SyncResult:
        raise http.client.IncompleteRead(b"")

    monkeypatch.setattr(employee_routes, "sync_employees", fake_sync_employees)

    with pytest.raises(HTTPException) as exc_info:
        await employee_routes.trigger_employee_sync(
            FakeSession([]),  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"finance_manager"})),
            mode="incremental",
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == (
        "iiko не отвечает — сервер оборвал соединение. Попробуйте через минуту."
    )

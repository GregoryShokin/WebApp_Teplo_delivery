from __future__ import annotations

import http.client
import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.deps import CurrentActor
from app.api.v1.routes import employees as employee_routes
from app.api.v1.routes.employees import (
    change_employee_pin,
    create_employee,
    dismiss_employee,
    list_employees,
    patch_employee,
    reinstate_employee,
)
from app.db.session import get_session
from app.main import create_app
from app.models import AgentAction, AgentRun, Employee, EmployeeRoleAssignment, ShiftLedgerEntry
from app.schemas.employees import (
    EmployeeCreateRequest,
    EmployeeDismissRequest,
    EmployeePinChangeRequest,
)
from app.services import iiko_sync as iiko_sync_service
from app.services.employee_status import compute_status, position_group_for_position
from app.services.iiko_sync import (
    IikoEmployeeCreateResult,
    IikoEmployeeOperationError,
    IikoEmployeeRole,
    SyncResult,
    _enrich_employee_records_with_roles,
    _request_iiko_with_incomplete_read_retry,
    plan_employee_sync,
    sync_employees,
)

SYNC_TODAY = date(2026, 5, 27)
SYNC_NOW = datetime(2026, 5, 27, 10, 0, tzinfo=UTC)


class FakeScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class FakeSession:
    def __init__(
        self,
        employees: list[Employee],
        shift_entries: list[ShiftLedgerEntry] | None = None,
    ) -> None:
        self.employees = employees
        self.shift_entries = shift_entries or []
        self.committed = False
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.executed: list[Any] = []
        self.rolled_back = False

    async def get(self, _model: Any, employee_id: uuid.UUID) -> Employee | None:
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
        if isinstance(item, Employee) and item not in self.employees:
            self.employees.append(item)
        if isinstance(item, EmployeeRoleAssignment):
            employee = next(
                (employee for employee in self.employees if employee.id == item.employee_id),
                None,
            )
            if employee is not None and item not in employee.role_assignments:
                employee.role_assignments.append(item)

    async def scalar(self, _query: Any) -> Any | None:
        return None

    async def scalars(self, query: Any) -> FakeScalarResult:
        sql = str(query.compile(compile_kwargs={"literal_binds": True}))
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
        pin_set_at=SYNC_NOW if pin_hash else None,
        is_senior=False,
        is_deputy_senior=False,
        created_at=SYNC_NOW,
        updated_at=SYNC_NOW,
    )


def make_assignment(employee: Employee, payroll_role: str = "sushi") -> EmployeeRoleAssignment:
    return EmployeeRoleAssignment(
        id=uuid.uuid4(),
        employee_id=employee.id,
        payroll_role=payroll_role,
        category="category_2",
        is_primary=True,
        effective_from=SYNC_TODAY,
        created_at=SYNC_NOW,
        updated_at=SYNC_NOW,
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
    assert result.as_dict() == {"created": 0, "updated": 0, "deactivated": 0}
    assert actions[0].action_type == "skipped: missing_name_or_position"
    assert actions[0].after_value == {
        "iiko_id": iiko_id,
        "reason": "missing_name_or_position",
    }


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


async def test_patch_employee_full_name_returns_400() -> None:
    session = FakeSession([make_employee(iiko_id="iiko-4", full_name="Readonly Name")])

    with pytest.raises(HTTPException) as exc_info:
        await patch_employee(
            session.employees[0].id,
            {"full_name": "Manual Name"},
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"finance_manager"})),
        )

    assert exc_info.value.status_code == 400


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
    assert updated.pin_hash is not None
    assert updated.pin_hash != "9876"
    assert updated.pin_hash != old_hash
    assert updated.pin_set_at is not None
    assert actions[-1].action_type == "change_pin"
    assert actions[-1].after_value["pin_changed"] is True


def test_employee_pin_change_request_rejects_non_four_digit_pin() -> None:
    with pytest.raises(ValueError):
        EmployeePinChangeRequest(pin_code="123")
    with pytest.raises(ValueError):
        EmployeePinChangeRequest(pin_code="12345")


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
    assignments = [item for item in session.added if isinstance(item, EmployeeRoleAssignment)]
    assert calls == [("Новый Сотрудник", "role-cook", "1234")]
    assert employee.iiko_id == "iiko-new"
    assert employee.full_name == "Новый Сотрудник"
    assert employee.position == "Повар"
    assert employee.category == "category_1"
    assert employee.default_cooking_station == "sushi"
    assert employee.is_senior is True
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
    assert employee.pin_hash is not None
    assert employee.pin_hash != "1234"
    assert employee.pin_set_at is not None


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
    assert updated.status == "inactive"
    assert updated.fire_date == SYNC_TODAY
    assert updated.fire_reason == "Переезд"
    assert session.committed is True
    assert len(actions) == 1
    assert actions[0].before_value["status"] == "active"
    assert actions[0].after_value["status"] == "inactive"
    assert actions[0].after_value["fire_reason"] == "Переезд"
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
        EmployeeDismissRequest(fire_date=SYNC_TODAY),
    )

    with pytest.raises(HTTPException) as exc_info:
        await dismiss_employee(
            employee.id,
            session,  # type: ignore[arg-type]
            actor,
            EmployeeDismissRequest(fire_date=SYNC_TODAY),
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
        EmployeeDismissRequest(fire_date=SYNC_TODAY),
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
            EmployeeDismissRequest(fire_date=SYNC_TODAY),
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
        EmployeeDismissRequest(fire_date=SYNC_TODAY),
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

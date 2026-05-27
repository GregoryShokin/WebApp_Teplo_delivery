from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.deps import CurrentActor
from app.api.v1.routes import employees as employee_routes
from app.api.v1.routes.employees import list_employees, patch_employee
from app.db.session import get_session
from app.main import create_app
from app.models import Employee
from app.services.iiko_sync import (
    SyncResult,
    _enrich_employee_records_with_roles,
    plan_employee_sync,
)

SYNC_TODAY = date(2026, 5, 27)
SYNC_NOW = datetime(2026, 5, 27, 10, 0, tzinfo=UTC)


class FakeScalarResult:
    def __init__(self, employees: list[Employee]) -> None:
        self._employees = employees

    def all(self) -> list[Employee]:
        return self._employees


class FakeSession:
    def __init__(self, employees: list[Employee]) -> None:
        self.employees = employees
        self.committed = False

    async def get(self, _model: Any, employee_id: uuid.UUID) -> Employee | None:
        return next((employee for employee in self.employees if employee.id == employee_id), None)

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _employee: Employee) -> None:
        return None

    async def scalars(self, query: Any) -> FakeScalarResult:
        sql = str(query.compile(compile_kwargs={"literal_binds": True}))
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
) -> Employee:
    return Employee(
        id=uuid.uuid4(),
        iiko_id=iiko_id,
        full_name=full_name,
        status=status,
        position=position,
        category=category,
        default_cooking_station=default_cooking_station,
        is_senior=False,
        is_deputy_senior=False,
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


def test_sync_skips_employee_outside_target_positions() -> None:
    existing: dict[str, Employee] = {}

    plan = plan_employee_sync(
        existing,
        [{"id": "iiko-1", "name": "Курьер", "position": "Курьер"}],
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


def test_sync_creates_lowercase_target_position_employee() -> None:
    existing: dict[str, Employee] = {}

    plan = plan_employee_sync(
        existing,
        [{"id": "iiko-1", "name": "Сушист", "position": "сушист"}],
        SYNC_TODAY,
        SYNC_NOW,
    )

    assert plan.result.created == 1
    assert existing["iiko-1"].position == "сушист"


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
        [{"id": "iiko-10", "name": "Бывший Повар", "position": "Курьер"}],
        SYNC_TODAY,
        SYNC_NOW,
    )

    assert plan.result.deactivated == 1
    assert existing["iiko-10"].status == "inactive"
    assert existing["iiko-10"].fire_date == SYNC_TODAY


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
        {"category": "category_1"},
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    assert updated.category == "category_1"
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
        {"category": "category_1"},
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    assert updated.category == "category_1"
    assert updated.default_cooking_station is None
    assert updated.status == "active"


async def test_patch_employee_cashier_cooking_station_returns_400() -> None:
    employee = make_employee(
        iiko_id="iiko-14",
        full_name="Cashier",
        status="active",
        position="Кассир",
        category="category_1",
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

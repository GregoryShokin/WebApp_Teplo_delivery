from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi import HTTPException

from app.api.deps import CurrentActor
from app.api.v1.routes.employees import list_employees, patch_employee
from app.models import Employee
from app.services.iiko_sync import plan_employee_sync

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
        if "employee.status = 'needs_setup'" in sql:
            employees = [employee for employee in employees if employee.status == "needs_setup"]
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
    category: str | None = "2",
) -> Employee:
    return Employee(
        id=uuid.uuid4(),
        iiko_id=iiko_id,
        full_name=full_name,
        status=status,
        position=position,
        category=category,
        is_senior=False,
        is_deputy_senior=False,
        created_at=SYNC_NOW,
        updated_at=SYNC_NOW,
    )


def test_sync_creates_new_employee_from_iiko() -> None:
    existing: dict[str, Employee] = {}

    plan = plan_employee_sync(
        existing,
        [{"id": "iiko-1", "name": "Новый Сотрудник"}],
        SYNC_TODAY,
        SYNC_NOW,
    )

    employee = existing["iiko-1"]
    assert plan.result.created == 1
    assert employee.full_name == "Новый Сотрудник"
    assert employee.status == "needs_setup"
    assert employee.position is None
    assert employee.category is None
    assert employee.is_senior is False


def test_sync_updates_full_name_for_existing_employee() -> None:
    existing = {
        "iiko-2": make_employee(iiko_id="iiko-2", full_name="Старое Имя"),
    }

    plan = plan_employee_sync(
        existing,
        [{"id": "iiko-2", "fullName": "Новое Имя"}],
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
            {"category": "3"},
            session,  # type: ignore[arg-type]
            CurrentActor(roles=frozenset({"manager"})),
        )

    assert exc_info.value.status_code == 403


async def test_patch_employee_category_finance_manager_ok() -> None:
    employee = make_employee(
        iiko_id="iiko-6",
        full_name="Needs Setup",
        status="needs_setup",
        position="Повар",
        category=None,
    )
    session = FakeSession([employee])

    updated = await patch_employee(
        employee.id,
        {"category": "4"},
        session,  # type: ignore[arg-type]
        CurrentActor(roles=frozenset({"finance_manager"})),
    )

    assert updated.category == "4"
    assert updated.status == "active"
    assert session.committed is True


async def test_get_filter_status_needs_setup_returns_only_unconfigured() -> None:
    session = FakeSession(
        [
            make_employee(
                iiko_id="iiko-7",
                full_name="Needs Setup",
                status="needs_setup",
                position=None,
                category=None,
            ),
            make_employee(iiko_id="iiko-8", full_name="Active Employee", status="active"),
            make_employee(iiko_id="iiko-9", full_name="Inactive Employee", status="inactive"),
        ]
    )

    employees = await list_employees(session, status_filter="needs_setup")  # type: ignore[arg-type]

    assert [employee.iiko_id for employee in employees] == ["iiko-7"]

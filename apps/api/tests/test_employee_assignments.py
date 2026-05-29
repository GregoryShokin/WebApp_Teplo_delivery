from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.api.deps import CurrentActor
from app.api.v1.routes.employees import (
    create_employee_assignment,
    delete_employee_assignment,
    patch_employee_assignment,
)
from app.core.config import get_settings
from app.models import Employee, EmployeeRoleAssignment
from app.schemas.employees import EmployeeRoleAssignmentCreate, EmployeeRoleAssignmentPatch
from app.services.employee_assignments import add_role, get_assignments
from app.services.payroll_config import (
    list_category_coefficients,
    list_rate_matrix,
    list_role_category_availability,
)

API_DIR = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get(
    "TEPLO_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+asyncpg://teplo:teplo@localhost:5432/teplo"),
)


async def _ping_database(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("select 1"))
    finally:
        await engine.dispose()


@pytest.fixture()
def postgres_available() -> None:
    try:
        asyncio.run(_ping_database(TEST_DATABASE_URL))
    except Exception as exc:
        pytest.skip(f"PostgreSQL test database is not available: {exc}")


@pytest.fixture()
def alembic_cfg(monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("TEPLO_ADMIN_PASSWORD", "test-admin-password")
    get_settings.cache_clear()

    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return cfg


@pytest.fixture()
def migrated_db(alembic_cfg: Config, postgres_available: None) -> str:
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")
    try:
        yield TEST_DATABASE_URL
    finally:
        command.downgrade(alembic_cfg, "base")


@pytest.fixture()
async def session_factory(migrated_db: str):
    engine = create_async_engine(migrated_db)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def test_post_new_role_creates_additional_assignment(session_factory) -> None:
    async with session_factory() as session:
        employee = await _create_employee(session)
        await add_role(session, employee.id, "sushi", "category_1", is_primary=True)

        assignment = await create_employee_assignment(
            employee.id,
            EmployeeRoleAssignmentCreate(payroll_role="pizza", category="category_2"),
            session,
            _finance_manager(),
        )
        assignments = await get_assignments(session, employee.id, date.today())

    assert assignment.payroll_role == "pizza"
    assert assignment.category == "category_2"
    assert len(assignments) == 2


async def test_employee_can_have_multiple_active_assignments(session_factory) -> None:
    async with session_factory() as session:
        employee = await _create_employee(session)
        await add_role(session, employee.id, "sushi", "category_1", is_primary=True)
        await add_role(session, employee.id, "pizza", "category_2")

        assignments = await get_assignments(session, employee.id, date.today())

    assert {assignment.payroll_role for assignment in assignments} == {"sushi", "pizza"}


async def test_only_one_assignment_is_primary_per_employee(session_factory) -> None:
    async with session_factory() as session:
        employee = await _create_employee(session)
        await add_role(session, employee.id, "sushi", "category_1", is_primary=True)
        pizza = await add_role(session, employee.id, "pizza", "category_2")

        await patch_employee_assignment(
            employee.id,
            pizza.id,
            EmployeeRoleAssignmentPatch(is_primary=True),
            session,
            _finance_manager(),
        )
        assignments = await get_assignments(session, employee.id, date.today())

    primary = [assignment for assignment in assignments if assignment.is_primary]
    assert len(primary) == 1
    assert primary[0].payroll_role == "pizza"


async def test_get_assignments_returns_only_active_on_date(session_factory) -> None:
    async with session_factory() as session:
        employee = await _create_employee(session)
        session.add_all(
            [
                EmployeeRoleAssignment(
                    employee_id=employee.id,
                    payroll_role="sushi",
                    category="category_1",
                    is_primary=True,
                    effective_from=date(2026, 1, 1),
                    effective_to=date(2026, 2, 1),
                ),
                EmployeeRoleAssignment(
                    employee_id=employee.id,
                    payroll_role="pizza",
                    category="category_2",
                    is_primary=False,
                    effective_from=date(2026, 2, 1),
                ),
            ]
        )
        await session.commit()

        january = await get_assignments(session, employee.id, date(2026, 1, 15))
        february = await get_assignments(session, employee.id, date(2026, 2, 15))

    assert [assignment.payroll_role for assignment in january] == ["sushi"]
    assert [assignment.payroll_role for assignment in february] == ["pizza"]


async def test_delete_primary_assignment_without_replacement_returns_400(
    session_factory,
) -> None:
    async with session_factory() as session:
        employee = await _create_employee(session)
        assignment = await add_role(session, employee.id, "sushi", "category_1", is_primary=True)

        with pytest.raises(HTTPException) as exc_info:
            await delete_employee_assignment(
                employee.id,
                assignment.id,
                session,
                _finance_manager(),
            )

    assert exc_info.value.status_code == 400


async def test_category_4_coefficient_is_seeded(session_factory) -> None:
    async with session_factory() as session:
        coefficients = await list_category_coefficients(session)

    by_category = {coefficient.category: coefficient.coefficient for coefficient in coefficients}
    assert str(by_category["category_4"]) == "2.500"


async def test_category_4_shawarma_rate_is_seeded(session_factory) -> None:
    async with session_factory() as session:
        rates = await list_rate_matrix(session, include_disabled=True)

    shawarma_category_4 = next(
        rate
        for rate in rates
        if rate["position_group"] == "Шаурмист" and rate["category"] == "category_4"
    )
    assert str(shawarma_category_4["amount"]) == "1800.00"
    assert shawarma_category_4["is_enabled"] is True


async def test_category_4_availability_is_shawarma_only(session_factory) -> None:
    async with session_factory() as session:
        availability = await list_role_category_availability(session)

    category_4 = {
        row["position_group"]: row["is_enabled"]
        for row in availability
        if row["category"] == "category_4"
    }
    assert category_4["Шаурмист"] is True
    assert all(
        is_enabled is False
        for position_group, is_enabled in category_4.items()
        if position_group != "Шаурмист"
    )


async def test_patch_shawarma_assignment_allows_category_4(session_factory) -> None:
    async with session_factory() as session:
        employee = await _create_employee(
            session,
            position="Повар",
            category="category_3",
            default_cooking_station="shawarma",
        )
        assignment = await add_role(session, employee.id, "shawarma", "category_3", is_primary=True)

        updated = await patch_employee_assignment(
            employee.id,
            assignment.id,
            EmployeeRoleAssignmentPatch(category="category_4"),
            session,
            _finance_manager(),
        )

    assert updated.category == "category_4"


async def test_patch_non_shawarma_assignment_rejects_category_4(session_factory) -> None:
    async with session_factory() as session:
        employee = await _create_employee(session)
        assignment = await add_role(session, employee.id, "sushi", "category_1", is_primary=True)

        with pytest.raises(HTTPException) as exc_info:
            await patch_employee_assignment(
                employee.id,
                assignment.id,
                EmployeeRoleAssignmentPatch(category="category_4"),
                session,
                _finance_manager(),
            )

    assert exc_info.value.status_code == 400
    expected_detail = "Категория недоступна для этой роли"
    assert exc_info.value.detail == expected_detail


async def _create_employee(
    session,
    *,
    position: str | None = "Повар",
    category: str | None = "category_1",
    default_cooking_station: str | None = "sushi",
) -> Employee:
    employee = Employee(
        id=uuid.uuid4(),
        iiko_id=f"iiko-{uuid.uuid4()}",
        full_name="Assignment Employee",
        position=position,
        category=category,
        default_cooking_station=default_cooking_station,
        status="active",
        is_senior=False,
        is_deputy_senior=False,
        pin_hash="hashed-pin",
        pin_set_at=datetime(2026, 5, 1, tzinfo=UTC),
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
        updated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    session.add(employee)
    await session.commit()
    return employee


def _finance_manager() -> CurrentActor:
    return CurrentActor(roles=frozenset({"finance_manager"}))

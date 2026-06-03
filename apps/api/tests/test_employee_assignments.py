from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.api.deps import CurrentActor
from app.api.v1.routes.employees import (
    create_employee_assignment,
    delete_employee_assignment,
    list_employee_assignments,
    list_employee_changes,
    patch_employee_assignment,
)
from app.core.config import get_settings
from app.models import (
    Employee,
    EmployeeChangeEvent,
    EmployeeRoleAssignment,
    PayrollLine,
    PayrollPeriod,
    PayrollRun,
)
from app.schemas.employees import EmployeeRoleAssignmentCreate, EmployeeRoleAssignmentPatch
from app.services import employee_assignments as employee_assignments_service
from app.services.employee_assignments import EmployeeAssignmentError, add_role, get_assignments
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
        event = await session.scalar(
            select(EmployeeChangeEvent).where(
                EmployeeChangeEvent.employee_id == employee.id,
                EmployeeChangeEvent.change_type == "assign_role",
            )
        )

    assert assignment.payroll_role == "pizza"
    assert assignment.category == "category_2"
    assert len(assignments) == 2
    assert event is not None
    assert event.source == "app"
    assert event.after_value["payroll_role"] == "pizza"


async def test_employee_changes_endpoint_filters_by_employee_source_status_type(
    session_factory,
) -> None:
    async with session_factory() as session:
        employee = await _create_employee(session, iiko_id="iiko-change-filter")
        other = await _create_employee(session, iiko_id="iiko-change-filter-other")
        matching_event = EmployeeChangeEvent(
            employee_id=employee.id,
            changed_at=datetime(2026, 5, 29, 10, 0, tzinfo=UTC),
            change_type="dismiss",
            source="app",
            actor_label="finance_manager",
            status="success",
            summary="Сотрудник уволен",
            before_value={"status": "active"},
            after_value={"status": "inactive"},
            diff={"status": {"before": "active", "after": "inactive"}},
            payroll_impact=True,
            payroll_impact_metadata={},
        )
        session.add_all(
            [
                matching_event,
                EmployeeChangeEvent(
                    employee_id=employee.id,
                    changed_at=datetime(2026, 5, 29, 11, 0, tzinfo=UTC),
                    change_type="dismiss",
                    source="iiko_sync",
                    actor_label="Синхронизация IIko",
                    status="success",
                    summary="Синхронизация IIko: сотрудник деактивирован",
                    payroll_impact=True,
                    payroll_impact_metadata={},
                ),
                EmployeeChangeEvent(
                    employee_id=other.id,
                    changed_at=datetime(2026, 5, 29, 12, 0, tzinfo=UTC),
                    change_type="dismiss",
                    source="app",
                    actor_label="finance_manager",
                    status="error",
                    summary="Ошибка",
                    payroll_impact=True,
                    payroll_impact_metadata={},
                ),
            ]
        )
        await session.commit()

        rows = await list_employee_changes(
            session,
            _finance_manager(),
            employee_id=employee.id,
            change_type="dismiss",
            source="app",
            event_status="success",
        )

    assert [row.id for row in rows] == [matching_event.id]


async def test_employee_can_have_multiple_active_assignments(session_factory) -> None:
    async with session_factory() as session:
        employee = await _create_employee(session)
        await add_role(session, employee.id, "sushi", "category_1", is_primary=True)
        await add_role(session, employee.id, "pizza", "category_2")

        assignments = await get_assignments(session, employee.id, date.today())

    assert {assignment.payroll_role for assignment in assignments} == {"sushi", "pizza"}


async def test_substitute_role_uses_configured_pair_without_becoming_primary(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synced_employee_ids: list[uuid.UUID] = []

    async def fake_sync(_session, employee_id: uuid.UUID) -> None:
        synced_employee_ids.append(employee_id)

    monkeypatch.setattr(
        employee_assignments_service,
        "_sync_employee_roles_to_iiko_safely",
        fake_sync,
    )

    async with session_factory() as session:
        employee = await _create_employee(
            session,
            position="Управляющий",
            category=None,
            default_cooking_station=None,
        )

        assignment = await add_role(
            session,
            employee.id,
            "sushi",
            "category_1",
            is_substitute=True,
        )
        assignments = await get_assignments(session, employee.id, date.today())

    assert assignment.is_substitute is True
    assert assignment.is_primary is False
    assert [item.payroll_role for item in assignments] == ["sushi"]
    assert synced_employee_ids == [employee.id]


async def test_substitute_role_rejects_unconfigured_pair(session_factory) -> None:
    async with session_factory() as session:
        employee = await _create_employee(
            session,
            position="Системный администратор",
            category=None,
            default_cooking_station=None,
        )

        with pytest.raises(EmployeeAssignmentError, match="не разрешена"):
            await add_role(
                session,
                employee.id,
                "administrator",
                "category_2",
                is_substitute=True,
            )


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


async def test_get_with_include_pending_returns_future(session_factory) -> None:
    async with session_factory() as session:
        employee = await _create_employee(session)
        today = date.today()
        future = today + timedelta(days=7)
        session.add_all(
            [
                EmployeeRoleAssignment(
                    employee_id=employee.id,
                    payroll_role="sushi",
                    category="category_1",
                    is_primary=False,
                    effective_from=today,
                    effective_to=future,
                ),
                EmployeeRoleAssignment(
                    employee_id=employee.id,
                    payroll_role="sushi",
                    category="category_2",
                    is_primary=False,
                    effective_from=future,
                ),
            ]
        )
        await session.commit()

        assignments = await list_employee_assignments(
            employee.id,
            session,
            on_date=today,
            include_pending=True,
        )

    assert [assignment.category for assignment in assignments] == ["category_1", "category_2"]
    assert assignments[0].is_pending is False
    assert assignments[1].is_pending is True


async def test_get_without_include_pending_returns_only_active(session_factory) -> None:
    async with session_factory() as session:
        employee = await _create_employee(session)
        today = date.today()
        future = today + timedelta(days=7)
        session.add_all(
            [
                EmployeeRoleAssignment(
                    employee_id=employee.id,
                    payroll_role="sushi",
                    category="category_1",
                    is_primary=False,
                    effective_from=today,
                    effective_to=future,
                ),
                EmployeeRoleAssignment(
                    employee_id=employee.id,
                    payroll_role="sushi",
                    category="category_2",
                    is_primary=False,
                    effective_from=future,
                ),
            ]
        )
        await session.commit()

        assignments = await list_employee_assignments(
            employee.id,
            session,
            on_date=today,
            include_pending=False,
        )

    assert [assignment.category for assignment in assignments] == ["category_1"]


async def test_patch_with_future_effective_from_creates_pending(session_factory) -> None:
    async with session_factory() as session:
        employee = await _create_employee(session)
        assignment = await add_role(session, employee.id, "sushi", "category_1", is_primary=True)
        future = date.today() + timedelta(days=7)

        updated = await patch_employee_assignment(
            employee.id,
            assignment.id,
            EmployeeRoleAssignmentPatch(category="category_2", effective_from=future),
            session,
            _finance_manager(),
        )
        rows = list(
            (
                await session.scalars(
                    select(EmployeeRoleAssignment)
                    .where(EmployeeRoleAssignment.employee_id == employee.id)
                    .order_by(EmployeeRoleAssignment.effective_from)
                )
            ).all()
        )

    assert updated.id != assignment.id
    assert rows[0].category == "category_1"
    assert rows[0].effective_to == future
    assert rows[1].category == "category_2"
    assert rows[1].effective_from == future
    assert rows[1].effective_to is None


async def test_cancel_pending_primary_with_previous_succeeds(session_factory) -> None:
    async with session_factory() as session:
        employee = await _create_employee(session)
        today = date.today()
        future = today + timedelta(days=7)
        previous = EmployeeRoleAssignment(
            employee_id=employee.id,
            payroll_role="sushi",
            category="category_1",
            is_primary=True,
            effective_from=today,
            effective_to=future,
        )
        pending = EmployeeRoleAssignment(
            employee_id=employee.id,
            payroll_role="sushi",
            category="category_2",
            is_primary=True,
            effective_from=future,
        )
        session.add_all([previous, pending])
        await session.commit()

        await delete_employee_assignment(
            employee.id,
            pending.id,
            session,
            _finance_manager(),
        )
        persisted_previous = await session.get(EmployeeRoleAssignment, previous.id)
        deleted_pending = await session.get(EmployeeRoleAssignment, pending.id)
        persisted_employee = await session.get(Employee, employee.id)

    assert persisted_previous is not None
    assert persisted_previous.effective_to is None
    assert persisted_previous.is_primary is True
    assert deleted_pending is None
    assert persisted_employee is not None
    assert persisted_employee.category == "category_1"


async def test_cancel_pending_non_primary_succeeds_always(session_factory) -> None:
    async with session_factory() as session:
        employee = await _create_employee(session)
        pending = EmployeeRoleAssignment(
            employee_id=employee.id,
            payroll_role="pizza",
            category="category_2",
            is_primary=False,
            effective_from=date.today() + timedelta(days=7),
        )
        session.add(pending)
        await session.commit()

        await delete_employee_assignment(
            employee.id,
            pending.id,
            session,
            _finance_manager(),
        )
        deleted_pending = await session.get(EmployeeRoleAssignment, pending.id)

    assert deleted_pending is None


async def test_delete_active_uses_remove_assignment(session_factory) -> None:
    async with session_factory() as session:
        employee = await _create_employee(session)
        today = date.today()
        assignment = EmployeeRoleAssignment(
            employee_id=employee.id,
            payroll_role="sushi",
            category="category_1",
            is_primary=False,
            effective_from=today,
        )
        session.add(assignment)
        await session.commit()

        await delete_employee_assignment(
            employee.id,
            assignment.id,
            session,
            _finance_manager(),
        )
        persisted = await session.get(EmployeeRoleAssignment, assignment.id)

    assert persisted is not None
    assert persisted.effective_to == today


async def test_cancel_pending_with_next_pending_chains_correctly(session_factory) -> None:
    async with session_factory() as session:
        employee = await _create_employee(session)
        today = date.today()
        first_date = today + timedelta(days=7)
        second_date = today + timedelta(days=14)
        previous = EmployeeRoleAssignment(
            employee_id=employee.id,
            payroll_role="sushi",
            category="category_1",
            is_primary=True,
            effective_from=today,
            effective_to=first_date,
        )
        first_pending = EmployeeRoleAssignment(
            employee_id=employee.id,
            payroll_role="sushi",
            category="category_2",
            is_primary=True,
            effective_from=first_date,
            effective_to=second_date,
        )
        second_pending = EmployeeRoleAssignment(
            employee_id=employee.id,
            payroll_role="sushi",
            category="category_3",
            is_primary=True,
            effective_from=second_date,
        )
        session.add_all([previous, first_pending, second_pending])
        await session.commit()

        await delete_employee_assignment(
            employee.id,
            first_pending.id,
            session,
            _finance_manager(),
        )
        persisted_previous = await session.get(EmployeeRoleAssignment, previous.id)
        deleted_first = await session.get(EmployeeRoleAssignment, first_pending.id)
        persisted_second = await session.get(EmployeeRoleAssignment, second_pending.id)

    assert persisted_previous is not None
    assert persisted_previous.effective_to == second_date
    assert persisted_previous.is_primary is True
    assert deleted_first is None
    assert persisted_second is not None


async def test_cancel_pending_primary_without_previous_fails_409_in_russian(
    session_factory,
) -> None:
    async with session_factory() as session:
        employee = await _create_employee(session)
        pending = EmployeeRoleAssignment(
            employee_id=employee.id,
            payroll_role="sushi",
            category="category_2",
            is_primary=True,
            effective_from=date.today() + timedelta(days=7),
        )
        session.add(pending)
        await session.commit()

        with pytest.raises(HTTPException) as exc_info:
            await delete_employee_assignment(
                employee.id,
                pending.id,
                session,
                _finance_manager(),
            )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == (
        "Эта роль — единственная основная. Сначала назначьте другую основную роль."
    )


async def test_delete_active_with_future_pending_correctly_handled(session_factory) -> None:
    async with session_factory() as session:
        employee = await _create_employee(session)
        today = date.today()
        future = today + timedelta(days=7)
        active = EmployeeRoleAssignment(
            employee_id=employee.id,
            payroll_role="sushi",
            category="category_1",
            is_primary=False,
            effective_from=today,
            effective_to=future,
        )
        pending = EmployeeRoleAssignment(
            employee_id=employee.id,
            payroll_role="sushi",
            category="category_2",
            is_primary=False,
            effective_from=future,
        )
        session.add_all([active, pending])
        await session.commit()

        await delete_employee_assignment(
            employee.id,
            active.id,
            session,
            _finance_manager(),
        )
        persisted_active = await session.get(EmployeeRoleAssignment, active.id)
        persisted_pending = await session.get(EmployeeRoleAssignment, pending.id)

    assert persisted_active is not None
    assert persisted_active.effective_to == today
    assert persisted_pending is not None
    assert persisted_pending.effective_from == future


async def test_backdated_assignment_patch_requires_comment(session_factory) -> None:
    async with session_factory() as session:
        employee = await _create_employee(session)
        assignment = await add_role(
            session,
            employee.id,
            "sushi",
            "category_1",
            is_primary=True,
            effective_from=date(2026, 1, 1),
        )

        with pytest.raises(HTTPException) as exc_info:
            await patch_employee_assignment(
                employee.id,
                assignment.id,
                EmployeeRoleAssignmentPatch(
                    category="category_2",
                    effective_from=date.today() - timedelta(days=1),
                ),
                session,
                _finance_manager(),
            )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "Для изменения задним числом комментарий обязателен"


async def test_backdated_assignment_over_finalized_payroll_requires_review_without_mutating_line(
    session_factory,
) -> None:
    async with session_factory() as session:
        employee = await _create_employee(session)
        assignment = await add_role(
            session,
            employee.id,
            "sushi",
            "category_1",
            is_primary=True,
            effective_from=date(2026, 1, 1),
        )
        period = PayrollPeriod(
            id=uuid.uuid4(),
            period_type="week",
            start_date=date(2026, 5, 18),
            end_date=date(2026, 5, 24),
            payroll_date=date(2026, 5, 25),
            status="finalized",
        )
        run = PayrollRun(
            id=uuid.uuid4(),
            period_id=period.id,
            started_at=datetime(2026, 5, 25, 10, 0, tzinfo=UTC),
            finished_at=datetime(2026, 5, 25, 10, 5, tzinfo=UTC),
            status="finalized",
            blocking_issues=[],
            summary={},
        )
        line = PayrollLine(
            id=uuid.uuid4(),
            run_id=run.id,
            employee_id=employee.id,
            role="sushi",
            base_pay=1000,
            premium=0,
            percent_pay=0,
            vacation_pay=0,
            fund_accrual=0,
            deduction=0,
            total_payable=1000,
            components={"days": []},
        )
        session.add_all([period, run, line])
        await session.commit()

        updated = await patch_employee_assignment(
            employee.id,
            assignment.id,
            EmployeeRoleAssignmentPatch(
                category="category_2",
                effective_from=date(2026, 5, 20),
                comment="Исправление категории за закрытую неделю",
            ),
            session,
            _finance_manager(),
        )
        event = await session.scalar(
            select(EmployeeChangeEvent).where(
                EmployeeChangeEvent.employee_id == employee.id,
                EmployeeChangeEvent.change_type == "change_category",
            )
        )
        persisted_line = await session.get(PayrollLine, line.id)

    assert updated.category == "category_2"
    assert event is not None
    assert event.status == "requires_review"
    assert event.payroll_impact_metadata["correction_pending"] is True
    assert str(persisted_line.base_pay) == "1000.00"


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


async def test_category_4_availability_matches_taxonomy(session_factory) -> None:
    async with session_factory() as session:
        availability = await list_role_category_availability(session)

    category_4 = {
        row["position_group"]: row["is_enabled"]
        for row in availability
        if row["category"] == "category_4"
    }
    enabled_for = {"Шаурмист", "Администратор"}
    assert category_4["Шаурмист"] is True
    assert category_4["Администратор"] is True
    assert all(
        is_enabled is False
        for position_group, is_enabled in category_4.items()
        if position_group not in enabled_for
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


async def test_auxiliary_employee_rejects_role_assignment(session_factory) -> None:
    async with session_factory() as session:
        employee = await _create_employee(
            session,
            position="Уборщица",
            category=None,
            default_cooking_station=None,
        )

        with pytest.raises(EmployeeAssignmentError) as exc_info:
            await add_role(session, employee.id, "sushi", "category_1", is_primary=True)

    assert str(exc_info.value) == "Роль не соответствует должности сотрудника"


async def _create_employee(
    session,
    *,
    iiko_id: str | None = None,
    position: str | None = "Повар",
    category: str | None = "category_1",
    default_cooking_station: str | None = "sushi",
) -> Employee:
    employee = Employee(
        id=uuid.uuid4(),
        iiko_id=iiko_id or f"iiko-{uuid.uuid4()}",
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

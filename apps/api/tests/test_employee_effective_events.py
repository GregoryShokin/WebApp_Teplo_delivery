from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import func, select

from app.api.deps import CurrentActor
from app.api.v1.routes.employees import (
    _active_premium_holders,
    list_employees,
    patch_employee,
)
from app.models import (
    Employee,
    EmployeeAllowanceEvent,
    EmployeeChangeEvent,
    EmployeePositionAssignment,
    EmployeeRoleAssignment,
)
from app.services import shift_schedule_service
from app.services.employee_effective_events import (
    get_allowances_for_employees_on_date,
    set_allowance,
)


def _employee(*, is_senior: bool) -> Employee:
    return Employee(
        id=uuid.uuid4(),
        full_name="Тестовый Старший",
        iiko_id=f"iiko-{uuid.uuid4()}",
        status="active",
        is_senior=is_senior,
        is_deputy_senior=False,
    )


async def test_backdated_removal_moves_existing_false_interval_and_repairs_snapshot(
    async_session_factory,
) -> None:
    today = date.today()
    employee = _employee(is_senior=True)
    enabled = EmployeeAllowanceEvent(
        employee_id=employee.id,
        allowance_type="senior",
        is_enabled=True,
        effective_from=today - timedelta(days=30),
        effective_to=today - timedelta(days=5),
    )
    removal = EmployeeAllowanceEvent(
        employee_id=employee.id,
        allowance_type="senior",
        is_enabled=False,
        effective_from=today - timedelta(days=5),
    )

    async with async_session_factory() as session:
        session.add_all([employee, enabled, removal])
        await session.flush()

        corrected_from = today - timedelta(days=7)
        event = await set_allowance(
            session,
            employee.id,
            "senior",
            False,
            effective_from=corrected_from,
            comment="снят с должности",
        )

        assert event is removal
        assert removal.effective_from == corrected_from
        assert removal.comment == "снят с должности"
        assert enabled.effective_to == corrected_from
        assert employee.is_senior is False
        assert (
            await session.scalar(
                select(func.count())
                .select_from(EmployeeAllowanceEvent)
                .where(EmployeeAllowanceEvent.employee_id == employee.id)
            )
            == 2
        )


async def test_repeating_active_allowance_state_is_noop_but_repairs_snapshot(
    async_session_factory,
) -> None:
    today = date.today()
    employee = _employee(is_senior=True)
    session_events = [
        EmployeeAllowanceEvent(
            employee_id=employee.id,
            allowance_type="senior",
            is_enabled=True,
            effective_from=today - timedelta(days=30),
            effective_to=today - timedelta(days=5),
        ),
        EmployeeAllowanceEvent(
            employee_id=employee.id,
            allowance_type="senior",
            is_enabled=False,
            effective_from=today - timedelta(days=5),
        ),
    ]

    async with async_session_factory() as session:
        session.add_all([employee, *session_events])
        await session.flush()

        event = await set_allowance(
            session,
            employee.id,
            "senior",
            False,
            effective_from=today,
        )

        assert event is None
        assert employee.is_senior is False
        assert (
            await session.scalar(
                select(func.count())
                .select_from(EmployeeAllowanceEvent)
                .where(EmployeeAllowanceEvent.employee_id == employee.id)
            )
            == 2
        )


async def test_bulk_allowance_lookup_uses_effective_events_over_stale_snapshot(
    async_session_factory,
) -> None:
    today = date.today()
    employee = _employee(is_senior=True)
    removal = EmployeeAllowanceEvent(
        employee_id=employee.id,
        allowance_type="senior",
        is_enabled=False,
        effective_from=today - timedelta(days=1),
    )

    async with async_session_factory() as session:
        session.add_all([employee, removal])
        await session.flush()

        flags = await get_allowances_for_employees_on_date(session, [employee], today)

        assert flags[employee.id] == {
            "is_senior": False,
            "is_deputy_senior": False,
        }


async def test_staff_and_schedule_read_effective_flags_instead_of_stale_snapshot(
    async_session_factory,
) -> None:
    today = date.today()
    employee = _employee(is_senior=True)
    employee.full_name = "Александр Чмыхов Тест"
    session_rows = [
        employee,
        EmployeePositionAssignment(
            employee_id=employee.id,
            position="Повар",
            effective_from=today - timedelta(days=30),
        ),
        EmployeeRoleAssignment(
            employee_id=employee.id,
            payroll_role="sushi",
            category="category_1",
            is_primary=True,
            effective_from=today - timedelta(days=30),
        ),
        EmployeeAllowanceEvent(
            employee_id=employee.id,
            allowance_type="senior",
            is_enabled=False,
            effective_from=today - timedelta(days=1),
        ),
    ]

    async with async_session_factory() as session:
        session.add_all(session_rows)
        await session.commit()

        staff_rows = await list_employees(session, search=employee.full_name)
        roster = await shift_schedule_service.list_employees_roster(session)

        staff_employee = next(row for row in staff_rows if row.id == employee.id)
        roster_employee = next(row for row in roster if row["id"] == employee.id)
        assert staff_employee.is_senior is False
        assert roster_employee["allowances"]["senior"] is False


async def test_stale_snapshot_does_not_block_assigning_a_new_senior(
    async_session_factory,
) -> None:
    today = date.today()
    employee = _employee(is_senior=True)
    session_rows = [
        employee,
        EmployeePositionAssignment(
            employee_id=employee.id,
            position="Повар",
            effective_from=today - timedelta(days=30),
        ),
        EmployeeAllowanceEvent(
            employee_id=employee.id,
            allowance_type="senior",
            is_enabled=False,
            effective_from=today - timedelta(days=1),
        ),
    ]

    async with async_session_factory() as session:
        session.add_all(session_rows)
        await session.commit()

        holders = await _active_premium_holders(
            session,
            "Повар",
            "is_senior",
            exclude_employee_id=None,
        )

        assert holders == []


async def test_repeated_removal_does_not_add_change_history_row(
    async_session_factory,
) -> None:
    today = date.today()
    employee = _employee(is_senior=True)
    session_rows = [
        employee,
        EmployeePositionAssignment(
            employee_id=employee.id,
            position="Повар",
            effective_from=today - timedelta(days=30),
        ),
        EmployeeRoleAssignment(
            employee_id=employee.id,
            payroll_role="sushi",
            category="category_1",
            is_primary=True,
            effective_from=today - timedelta(days=30),
        ),
        EmployeeAllowanceEvent(
            employee_id=employee.id,
            allowance_type="senior",
            is_enabled=False,
            effective_from=today - timedelta(days=1),
        ),
    ]

    async with async_session_factory() as session:
        session.add_all(session_rows)
        await session.commit()

        updated = await patch_employee(
            employee.id,
            {"is_senior": False, "effective_from": today},
            session,
            CurrentActor(roles=frozenset({"manager"})),
        )

        assert updated.is_senior is False
        change_types = list(
            (
                await session.scalars(
                    select(EmployeeChangeEvent.change_type).where(
                        EmployeeChangeEvent.employee_id == employee.id
                    )
                )
            ).all()
        )
        assert "unset_senior" not in change_types

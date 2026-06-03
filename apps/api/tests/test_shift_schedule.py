from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.deps import CurrentActor
from app.api.v1.routes.shift_schedule import _shift_to_read
from app.db.session import get_session
from app.main import create_app
from app.models import (
    Employee,
    EmployeeRoleAssignment,
    ScheduledShift,
    ShiftLedgerEntry,
    ShiftSchedule,
)
from app.services import payroll_config, shift_schedule_service
from app.services.staff_taxonomy import default_station_for_payroll_role


class FakeScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class FakeExecuteResult(FakeScalarResult):
    pass


class ShiftScheduleFakeSession:
    def __init__(
        self,
        *,
        schedules: list[ShiftSchedule] | None = None,
        employees: list[Employee] | None = None,
        shifts: list[ScheduledShift] | None = None,
        ledger_entries: list[ShiftLedgerEntry] | None = None,
        assignments: list[EmployeeRoleAssignment] | None = None,
    ) -> None:
        self.schedules = {item.id: item for item in schedules or []}
        self.employees = {item.id: item for item in employees or []}
        self.shifts = {item.id: item for item in shifts or []}
        self.ledger_entries = {item.id: item for item in ledger_entries or []}
        self.assignments = assignments or []
        self.commits = 0
        self.rollbacks = 0
        self.refreshed: list[Any] = []

    def add(self, item: Any) -> None:
        if isinstance(item, ShiftSchedule):
            self.schedules[item.id] = item
        elif isinstance(item, ScheduledShift):
            self.shifts[item.id] = item
        elif isinstance(item, Employee):
            self.employees[item.id] = item
        elif isinstance(item, EmployeeRoleAssignment):
            self.assignments.append(item)

    async def get(self, model: Any, item_id: uuid.UUID) -> Any | None:
        if model is ShiftSchedule:
            return self.schedules.get(item_id)
        if model is ScheduledShift:
            return self.shifts.get(item_id)
        if model is ShiftLedgerEntry:
            return self.ledger_entries.get(item_id)
        if model is Employee:
            return self.employees.get(item_id)
        return None

    async def scalar(self, query: Any) -> Any | None:
        del query
        return None

    async def scalars(self, query: Any) -> FakeScalarResult:
        entity = query_entity(query)
        if entity is ShiftSchedule:
            return FakeScalarResult(list(self.schedules.values()))
        if entity is ScheduledShift:
            return FakeScalarResult(list(self.shifts.values()))
        if entity is Employee:
            return FakeScalarResult(
                [employee for employee in self.employees.values() if employee.status == "active"]
            )
        if entity is EmployeeRoleAssignment:
            return FakeScalarResult(self.assignments)
        return FakeScalarResult([])

    async def execute(self, query: Any) -> FakeExecuteResult:
        entities = query_entities(query)
        if entities[:2] == [ScheduledShift, Employee]:
            return FakeExecuteResult(
                [
                    (shift, self.employees[shift.employee_id])
                    for shift in self.shifts.values()
                    if shift.employee_id in self.employees
                ]
            )
        if entities[:2] == [ShiftLedgerEntry, Employee]:
            return FakeExecuteResult(
                [
                    (entry, self.employees[entry.employee_id])
                    for entry in self.ledger_entries.values()
                    if entry.employee_id in self.employees
                ]
            )
        if entities[:2] == [Employee, EmployeeRoleAssignment]:
            today = date.today()
            rows = []
            for employee in self.employees.values():
                assignment = next(
                    (
                        item
                        for item in self.assignments
                        if item.employee_id == employee.id
                        and item.is_primary
                        and item.effective_from <= today
                        and (item.effective_to is None or item.effective_to > today)
                    ),
                    None,
                )
                rows.append((employee, assignment))
            return FakeExecuteResult(rows)
        return FakeExecuteResult([])

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def refresh(self, item: Any) -> None:
        self.refreshed.append(item)

    async def delete(self, item: Any) -> None:
        if isinstance(item, ShiftSchedule):
            self.schedules.pop(item.id, None)
            self.shifts = {
                shift_id: shift
                for shift_id, shift in self.shifts.items()
                if shift.shift_schedule_id != item.id
            }
        elif isinstance(item, ScheduledShift):
            self.shifts.pop(item.id, None)


def query_entity(query: Any) -> Any | None:
    entities = query_entities(query)
    return entities[0] if entities else None


def query_entities(query: Any) -> list[Any]:
    return [
        description.get("entity")
        for description in (getattr(query, "column_descriptions", None) or [])
    ]


def actor() -> CurrentActor:
    return CurrentActor(roles=frozenset({"finance_manager"}))


def schedule(
    *,
    status: str = "draft",
    date_start: date = date(2026, 6, 1),
    date_end: date = date(2026, 6, 30),
) -> ShiftSchedule:
    return ShiftSchedule(
        id=uuid.uuid4(),
        date_start=date_start,
        date_end=date_end,
        status=status,
        notes=None,
    )


def employee(
    *,
    position: str = "Повар",
    status: str = "active",
    full_name: str = "Иванов Иван",
    is_senior: bool = False,
    is_deputy_senior: bool = False,
) -> Employee:
    return Employee(
        id=uuid.uuid4(),
        full_name=full_name,
        iiko_id=f"iiko-{uuid.uuid4()}",
        position=position,
        status=status,
        default_cooking_station="pizza" if position == "Повар" else None,
        is_senior=is_senior,
        is_deputy_senior=is_deputy_senior,
    )


def assignment(
    employee_id: uuid.UUID,
    *,
    payroll_role: str = "pizza",
    category: str = "category_2",
    is_primary: bool = True,
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


def shift(
    schedule_id: uuid.UUID,
    employee_id: uuid.UUID,
    *,
    business_date: date = date(2026, 6, 2),
    start_hour: int = 10,
    end_hour: int = 22,
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
        employee_id=employee_id,
        payroll_role="Повар",
        station_code="Пицца",
        planned_start_at=start,
        planned_end_at=end,
    )


def ledger_entry(
    employee_id: uuid.UUID,
    *,
    work_date: date = date(2026, 6, 2),
    payroll_role: str | None = "sushi",
    opened_hour: int = 10,
    closed_hour: int | None = 22,
) -> ShiftLedgerEntry:
    opened_at = datetime(work_date.year, work_date.month, work_date.day, opened_hour, tzinfo=UTC)
    closed_at = (
        datetime(work_date.year, work_date.month, work_date.day, closed_hour, tzinfo=UTC)
        if closed_hour is not None
        else None
    )
    return ShiftLedgerEntry(
        id=uuid.uuid4(),
        work_date=work_date,
        employee_id=employee_id,
        payroll_role=payroll_role,
        category="category_2" if payroll_role else None,
        source="schedule" if payroll_role else "fallback_primary",
        opened_at=opened_at,
        closed_at=closed_at,
        is_resolved=payroll_role is not None,
    )


async def test_create_schedule_in_draft() -> None:
    session = ShiftScheduleFakeSession()

    created = await shift_schedule_service.create_schedule(
        session,  # type: ignore[arg-type]
        date_start=date(2026, 6, 1),
        date_end=date(2026, 6, 30),
        notes="Июнь",
        actor=actor(),
    )

    assert created.status == "draft"
    assert created.date_end >= created.date_start
    assert session.schedules[created.id] is created


async def test_create_schedule_invalid_date_range_fails_422() -> None:
    session = ShiftScheduleFakeSession()

    with pytest.raises(HTTPException) as exc_info:
        await shift_schedule_service.create_schedule(
            session,  # type: ignore[arg-type]
            date_start=date(2026, 6, 30),
            date_end=date(2026, 6, 1),
            notes=None,
            actor=actor(),
        )

    assert exc_info.value.status_code == 422


async def test_upsert_shift_for_non_payroll_position_fails_422() -> None:
    sched = schedule()
    manager = employee(position="Управляющий")
    session = ShiftScheduleFakeSession(schedules=[sched], employees=[manager])

    with pytest.raises(HTTPException) as exc_info:
        await shift_schedule_service.upsert_shift(
            session,  # type: ignore[arg-type]
            sched.id,
            business_date=date(2026, 6, 2),
            employee_id=manager.id,
            station_code=None,
            planned_start_at=datetime(2026, 6, 2, 10, tzinfo=UTC),
            planned_end_at=datetime(2026, 6, 2, 22, tzinfo=UTC),
            comment_private=None,
            actor=actor(),
        )

    assert exc_info.value.status_code == 422


async def test_upsert_shift_in_published_schedule_fails_409() -> None:
    sched = schedule(status="published")
    cook = employee()
    session = ShiftScheduleFakeSession(schedules=[sched], employees=[cook])

    with pytest.raises(HTTPException) as exc_info:
        await shift_schedule_service.upsert_shift(
            session,  # type: ignore[arg-type]
            sched.id,
            business_date=date(2026, 6, 2),
            employee_id=cook.id,
            station_code="Пицца",
            planned_start_at=datetime(2026, 6, 2, 10, tzinfo=UTC),
            planned_end_at=datetime(2026, 6, 2, 22, tzinfo=UTC),
            comment_private=None,
            actor=actor(),
        )

    assert exc_info.value.status_code == 409


async def test_unique_employee_per_day() -> None:
    sched = schedule()
    cook = employee()
    session = ShiftScheduleFakeSession(
        schedules=[sched],
        employees=[cook],
        assignments=[assignment(cook.id, payroll_role="pizza")],
    )

    first = await shift_schedule_service.upsert_shift(
        session,  # type: ignore[arg-type]
        sched.id,
        business_date=date(2026, 6, 2),
        employee_id=cook.id,
        station_code="Пицца",
        planned_start_at=datetime(2026, 6, 2, 10, tzinfo=UTC),
        planned_end_at=datetime(2026, 6, 2, 22, tzinfo=UTC),
        comment_private=None,
        actor=actor(),
    )
    second = await shift_schedule_service.upsert_shift(
        session,  # type: ignore[arg-type]
        sched.id,
        business_date=date(2026, 6, 2),
        employee_id=cook.id,
        station_code="Роллы",
        planned_start_at=datetime(2026, 6, 2, 11, tzinfo=UTC),
        planned_end_at=datetime(2026, 6, 2, 23, tzinfo=UTC),
        comment_private="обновлено",
        actor=actor(),
    )

    assert first.id == second.id
    assert len(session.shifts) == 1
    assert second.station_code == "Роллы"
    assert second.comment_private == "обновлено"


async def test_upsert_shift_with_no_optional_fields_uses_defaults() -> None:
    sched = schedule()
    cook = employee()
    session = ShiftScheduleFakeSession(
        schedules=[sched],
        employees=[cook],
        assignments=[assignment(cook.id, payroll_role="pizza")],
    )

    created = await shift_schedule_service.upsert_shift(
        session,  # type: ignore[arg-type]
        sched.id,
        business_date=date(2026, 6, 2),
        employee_id=cook.id,
        station_code=None,
        planned_start_at=None,
        planned_end_at=None,
        comment_private=None,
        actor=actor(),
    )

    assert created.payroll_role == "pizza"
    assert created.station_code == "Пицца"
    assert created.planned_start_at.hour == 10
    assert created.planned_end_at.hour == 22
    assert created.planned_start_at.tzinfo is not None


async def test_upsert_shift_with_no_assignments_fails_422() -> None:
    sched = schedule()
    cook = employee()
    session = ShiftScheduleFakeSession(schedules=[sched], employees=[cook])

    with pytest.raises(HTTPException) as exc_info:
        await shift_schedule_service.upsert_shift(
            session,  # type: ignore[arg-type]
            sched.id,
            business_date=date(2026, 6, 2),
            employee_id=cook.id,
            station_code=None,
            planned_start_at=None,
            planned_end_at=None,
            comment_private=None,
            actor=actor(),
        )

    assert exc_info.value.status_code == 422
    assert "не назначена ни одна роль" in exc_info.value.detail


async def test_upsert_shift_with_role_not_in_assignments_fails_422() -> None:
    sched = schedule()
    cook = employee()
    session = ShiftScheduleFakeSession(
        schedules=[sched],
        employees=[cook],
        assignments=[assignment(cook.id, payroll_role="pizza")],
    )

    with pytest.raises(HTTPException) as exc_info:
        await shift_schedule_service.upsert_shift(
            session,  # type: ignore[arg-type]
            sched.id,
            business_date=date(2026, 6, 2),
            employee_id=cook.id,
            payroll_role="administrator",
            station_code=None,
            planned_start_at=datetime(2026, 6, 2, 10, tzinfo=UTC),
            planned_end_at=datetime(2026, 6, 2, 22, tzinfo=UTC),
            comment_private=None,
            actor=actor(),
        )

    assert exc_info.value.status_code == 422
    assert "не назначена этому сотруднику" in exc_info.value.detail


async def test_upsert_shift_with_explicit_role_uses_it() -> None:
    sched = schedule()
    cook = employee()
    session = ShiftScheduleFakeSession(
        schedules=[sched],
        employees=[cook],
        assignments=[
            assignment(cook.id, payroll_role="pizza", is_primary=True),
            assignment(cook.id, payroll_role="sushi", is_primary=False),
        ],
    )

    created = await shift_schedule_service.upsert_shift(
        session,  # type: ignore[arg-type]
        sched.id,
        business_date=date(2026, 6, 2),
        employee_id=cook.id,
        payroll_role="sushi",
        station_code=None,
        planned_start_at=None,
        planned_end_at=None,
        comment_private=None,
        actor=actor(),
    )

    assert created.payroll_role == "sushi"
    assert created.station_code == "Роллы"


async def test_upsert_shift_explicit_times_override_defaults() -> None:
    sched = schedule()
    cook = employee()
    start = datetime(2026, 6, 2, 8, tzinfo=UTC)
    end = datetime(2026, 6, 2, 18, tzinfo=UTC)
    session = ShiftScheduleFakeSession(
        schedules=[sched],
        employees=[cook],
        assignments=[assignment(cook.id, payroll_role="pizza")],
    )

    created = await shift_schedule_service.upsert_shift(
        session,  # type: ignore[arg-type]
        sched.id,
        business_date=date(2026, 6, 2),
        employee_id=cook.id,
        station_code="Пицца",
        planned_start_at=start,
        planned_end_at=end,
        comment_private=None,
        actor=actor(),
    )

    assert created.planned_start_at == start
    assert created.planned_end_at == end


async def test_publish_supersedes_overlapping() -> None:
    previous = schedule(
        status="published",
        date_start=date(2026, 6, 1),
        date_end=date(2026, 6, 7),
    )
    replacement = schedule(
        status="draft",
        date_start=date(2026, 6, 5),
        date_end=date(2026, 6, 12),
    )
    session = ShiftScheduleFakeSession(schedules=[previous, replacement])

    published = await shift_schedule_service.publish_schedule(
        session,  # type: ignore[arg-type]
        replacement.id,
        actor=actor(),
    )

    assert published.status == "published"
    assert previous.status == "superseded"
    assert previous.superseded_by_id == replacement.id


async def test_publish_no_overlap_just_publishes() -> None:
    previous = schedule(
        status="published",
        date_start=date(2026, 6, 1),
        date_end=date(2026, 6, 7),
    )
    next_schedule = schedule(
        status="draft",
        date_start=date(2026, 6, 8),
        date_end=date(2026, 6, 12),
    )
    session = ShiftScheduleFakeSession(schedules=[previous, next_schedule])

    await shift_schedule_service.publish_schedule(
        session,  # type: ignore[arg-type]
        next_schedule.id,
        actor=actor(),
    )

    assert previous.status == "published"
    assert next_schedule.status == "published"


async def test_new_version_clones_shifts() -> None:
    source = schedule(status="published")
    cook = employee()
    shifts = [
        shift(source.id, cook.id, business_date=date(2026, 6, 1) + timedelta(days=index))
        for index in range(10)
    ]
    session = ShiftScheduleFakeSession(schedules=[source], employees=[cook], shifts=shifts)

    clone = await shift_schedule_service.create_new_version(
        session,  # type: ignore[arg-type]
        source.id,
        actor=actor(),
    )

    cloned_shifts = [
        item for item in session.shifts.values() if item.shift_schedule_id == clone.id
    ]
    assert clone.status == "draft"
    assert source.status == "published"
    assert len(cloned_shifts) == 10


async def test_copy_week_copies_all_shifts() -> None:
    sched = schedule()
    cook = employee()
    week_start = date(2026, 6, 2)
    shifts = [
        shift(sched.id, cook.id, business_date=week_start + timedelta(days=index))
        for index in range(5)
    ]
    session = ShiftScheduleFakeSession(schedules=[sched], employees=[cook], shifts=shifts)

    copied = await shift_schedule_service.bulk_copy_week(
        session,  # type: ignore[arg-type]
        sched.id,
        from_date=week_start,
        to_date=week_start + timedelta(days=7),
        actor=actor(),
    )

    target_dates = {
        item.business_date
        for item in session.shifts.values()
        if item.business_date >= week_start + timedelta(days=7)
    }
    assert copied == 5
    assert len(target_dates) == 5


async def test_delete_shift_in_published_fails_409() -> None:
    sched = schedule(status="published")
    cook = employee()
    scheduled = shift(sched.id, cook.id)
    session = ShiftScheduleFakeSession(
        schedules=[sched],
        employees=[cook],
        shifts=[scheduled],
    )

    with pytest.raises(HTTPException) as exc_info:
        await shift_schedule_service.delete_shift(
            session,  # type: ignore[arg-type]
            scheduled.id,
            actor=actor(),
        )

    assert exc_info.value.status_code == 409


def test_planned_hours_computed_in_response() -> None:
    sched = schedule()
    cook = employee()
    scheduled = shift(sched.id, cook.id)

    response = _shift_to_read(scheduled, cook)

    assert response.planned_hours == Decimal("12")


async def test_employees_roster_filters_payroll_positions() -> None:
    cook = employee(position="Повар", full_name="Повар Активный", is_senior=True)
    cashier = employee(position="Кассир", full_name="Кассир Активный")
    manager = employee(position="Управляющий", full_name="Управляющий")
    courier = employee(position="Курьер", full_name="Курьер")
    inactive = employee(position="Повар", status="inactive", full_name="Повар Уволенный")
    session = ShiftScheduleFakeSession(
        employees=[cook, cashier, manager, courier, inactive],
        assignments=[
            assignment(cook.id, payroll_role="pizza"),
            assignment(cashier.id, payroll_role="administrator"),
        ],
    )

    roster = await shift_schedule_service.list_employees_roster(session)  # type: ignore[arg-type]

    assert {row["full_name"] for row in roster} == {"Повар Активный", "Кассир Активный"}
    cook_row = next(row for row in roster if row["id"] == cook.id)
    assert cook_row["primary_payroll_role"] == "pizza"
    assert cook_row["allowances"]["senior"] is True


async def test_roster_returns_available_roles_with_primary_flag() -> None:
    cook = employee(position="Повар", full_name="Повар Активный")
    session = ShiftScheduleFakeSession(
        employees=[cook],
        assignments=[
            assignment(cook.id, payroll_role="pizza", is_primary=True),
            assignment(cook.id, payroll_role="sushi", category="category_3", is_primary=False),
        ],
    )

    roster = await shift_schedule_service.list_employees_roster(session)  # type: ignore[arg-type]

    roles = next(row for row in roster if row["id"] == cook.id)["available_roles"]
    assert roles == [
        {
            "payroll_role": "pizza",
            "category": "category_2",
            "is_primary": True,
            "is_substitute": False,
            "default_station_code": "Пицца",
        },
        {
            "payroll_role": "sushi",
            "category": "category_3",
            "is_primary": False,
            "is_substitute": False,
            "default_station_code": "Роллы",
        },
    ]


async def test_roster_hides_admin_with_substitute_role_when_pair_not_added_to_schedule() -> None:
    manager = employee(position="Управляющий", full_name="Управляющий Подмена")
    session = ShiftScheduleFakeSession(
        employees=[manager],
        assignments=[
            assignment(
                manager.id,
                payroll_role="sushi",
                category="category_1",
                is_primary=False,
                is_substitute=True,
            )
        ],
    )

    roster = await shift_schedule_service.list_employees_roster(session)  # type: ignore[arg-type]

    assert roster == []


def test_business_today_uses_location_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz == shift_schedule_service.LOCATION_TZ
            return cls(2026, 6, 3, 0, 30, tzinfo=tz)

    monkeypatch.setattr(shift_schedule_service, "datetime", FakeDateTime)

    assert shift_schedule_service._business_today() == date(2026, 6, 3)


async def test_roster_includes_admin_with_substitute_role_when_pair_added_to_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def get_substitute_pairs(_session):
        return [
            payroll_config.SubstitutePair(
                from_position="Управляющий",
                to_position="Повар",
                add_to_schedule=True,
            )
        ]

    monkeypatch.setattr(
        shift_schedule_service.payroll_config,
        "get_substitute_pairs",
        get_substitute_pairs,
    )
    manager = employee(position="Управляющий", full_name="Управляющий Подмена")
    session = ShiftScheduleFakeSession(
        employees=[manager],
        assignments=[
            assignment(
                manager.id,
                payroll_role="sushi",
                category="category_1",
                is_primary=False,
                is_substitute=True,
            )
        ],
    )

    roster = await shift_schedule_service.list_employees_roster(session)  # type: ignore[arg-type]

    assert [row["full_name"] for row in roster] == ["Управляющий Подмена"]
    assert roster[0]["primary_payroll_role"] is None
    assert roster[0]["available_roles"] == [
        {
            "payroll_role": "sushi",
            "category": "category_1",
            "is_primary": False,
            "is_substitute": True,
            "default_station_code": "Роллы",
        }
    ]


async def test_roster_returns_empty_available_roles_for_employee_without_assignments() -> None:
    cook = employee(position="Повар", full_name="Повар без роли")
    session = ShiftScheduleFakeSession(employees=[cook])

    roster = await shift_schedule_service.list_employees_roster(session)  # type: ignore[arg-type]

    cook_row = next(row for row in roster if row["id"] == cook.id)
    assert cook_row["primary_payroll_role"] is None
    assert cook_row["available_roles"] == []


def test_default_station_mapping() -> None:
    assert default_station_for_payroll_role("pizza") == "Пицца"
    assert default_station_for_payroll_role("administrator") == "Касса"
    assert default_station_for_payroll_role("prep") is None
    assert default_station_for_payroll_role("Пиццерист") == "Пицца"


def test_station_mapping_no_shaurma() -> None:
    assert default_station_for_payroll_role("Шаурмист") == "Горячий цех"
    assert default_station_for_payroll_role("shawarma") == "Горячий цех"


def test_ledger_endpoint_returns_entries_in_range() -> None:
    cook = employee(position="Повар", full_name="Повар Факт")
    entry = ledger_entry(cook.id, work_date=date(2026, 6, 2), payroll_role="sushi")
    outside_entry = ledger_entry(cook.id, work_date=date(2026, 6, 5), payroll_role="pizza")
    session = ShiftScheduleFakeSession(
        employees=[cook],
        ledger_entries=[entry, outside_entry],
    )
    app = create_app()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/schedule/ledger",
                params={"date_from": "2026-06-01", "date_to": "2026-06-03"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": str(entry.id),
            "business_date": "2026-06-02",
            "employee_id": str(cook.id),
            "employee_full_name": "Повар Факт",
            "position": "Повар",
            "payroll_role": "sushi",
            "station_code": "Роллы",
            "opened_at": "2026-06-02T10:00:00Z",
            "closed_at": "2026-06-02T22:00:00Z",
            "minutes_worked": 720,
            "is_closed": True,
        }
    ]


def test_ledger_endpoint_includes_entries_with_payroll_roles() -> None:
    cook = employee(position="Повар", full_name="Повар Факт")
    manager = employee(position="Управляющий", full_name="Управляющий Факт")
    courier = employee(position="Курьер", full_name="Курьер Факт")
    session = ShiftScheduleFakeSession(
        employees=[cook, manager, courier],
        ledger_entries=[
            ledger_entry(cook.id, work_date=date(2026, 6, 2), payroll_role="sushi"),
            ledger_entry(manager.id, work_date=date(2026, 6, 2), payroll_role="administrator"),
            ledger_entry(courier.id, work_date=date(2026, 6, 2), payroll_role=None),
        ],
    )
    app = create_app()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/schedule/ledger",
                params={"date_from": "2026-06-01", "date_to": "2026-06-03"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert [row["employee_full_name"] for row in response.json()] == [
        "Повар Факт",
        "Управляющий Факт",
    ]


async def test_shift_interval_longer_than_16h_fails_422() -> None:
    sched = schedule()
    cook = employee()
    session = ShiftScheduleFakeSession(
        schedules=[sched],
        employees=[cook],
        assignments=[assignment(cook.id, payroll_role="pizza")],
    )

    with pytest.raises(HTTPException) as exc_info:
        await shift_schedule_service.upsert_shift(
            session,  # type: ignore[arg-type]
            sched.id,
            business_date=date(2026, 6, 2),
            employee_id=cook.id,
            station_code="Пицца",
            planned_start_at=datetime(2026, 6, 2, 6, tzinfo=UTC),
            planned_end_at=datetime(2026, 6, 2, 23, tzinfo=UTC),
            comment_private=None,
            actor=actor(),
        )

    assert exc_info.value.status_code == 422

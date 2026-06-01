from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi import HTTPException

from app.api.deps import CurrentActor
from app.models import (
    AgentAction,
    AgentRun,
    AppSetting,
    Employee,
    ScheduledShift,
    ShiftSchedule,
    VacationPeriod,
)
from app.services import vacation_service
from app.services.vacation_service import VacationShiftConflictError


class FakeScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class FakeExecuteResult(FakeScalarResult):
    pass


class VacationFakeSession:
    def __init__(
        self,
        *,
        employees: list[Employee] | None = None,
        vacations: list[VacationPeriod] | None = None,
        schedules: list[ShiftSchedule] | None = None,
        shifts: list[ScheduledShift] | None = None,
        settings: list[AppSetting] | None = None,
    ) -> None:
        self.employees = {item.id: item for item in employees or []}
        self.vacations = {item.id: item for item in vacations or []}
        self.schedules = {item.id: item for item in schedules or []}
        self.shifts = {item.id: item for item in shifts or []}
        self.settings = {item.key: item for item in settings or []}
        self.agent_runs: list[AgentRun] = []
        self.agent_actions: list[AgentAction] = []
        self.commits = 0

    def add(self, item: Any) -> None:
        if isinstance(item, VacationPeriod):
            self.vacations[item.id] = item
        elif isinstance(item, Employee):
            self.employees[item.id] = item
        elif isinstance(item, AgentRun):
            self.agent_runs.append(item)
        elif isinstance(item, AgentAction):
            self.agent_actions.append(item)

    async def get(self, model: Any, item_id: uuid.UUID) -> Any | None:
        if model is Employee:
            return self.employees.get(item_id)
        if model is VacationPeriod:
            return self.vacations.get(item_id)
        if model is ScheduledShift:
            return self.shifts.get(item_id)
        if model is ShiftSchedule:
            return self.schedules.get(item_id)
        return None

    async def scalars(self, query: Any) -> FakeScalarResult:
        entity = query_entity(query)
        if entity is VacationPeriod:
            return FakeScalarResult(list(self.vacations.values()))
        if entity is AppSetting:
            return FakeScalarResult(list(self.settings.values()))
        if entity is Employee:
            return FakeScalarResult(list(self.employees.values()))
        return FakeScalarResult([])

    async def scalar(self, query: Any) -> Any | None:
        rows = (await self.scalars(query)).all()
        return rows[0] if rows else None

    async def execute(self, query: Any) -> FakeExecuteResult:
        entities = query_entities(query)
        if entities[:2] == [ScheduledShift, ShiftSchedule]:
            return FakeExecuteResult(
                [
                    (shift, self.schedules[shift.shift_schedule_id])
                    for shift in self.shifts.values()
                    if shift.shift_schedule_id in self.schedules
                ]
            )
        return FakeExecuteResult([])

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, item: Any) -> None:
        return None

    async def delete(self, item: Any) -> None:
        if isinstance(item, ScheduledShift):
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
    return CurrentActor(roles=frozenset({"finance_manager"}), user_id=uuid.uuid4())


def employee(*, position: str = "Повар", status: str = "active") -> Employee:
    return Employee(
        id=uuid.uuid4(),
        full_name="Vacation Employee",
        iiko_id=f"iiko-{uuid.uuid4()}",
        position=position,
        status=status,
    )


def vacation_setting(key: str, value: Any) -> AppSetting:
    return AppSetting(
        id=uuid.uuid4(),
        key=key,
        value=value,
        value_type="number",
        category="vacation",
        display_name=key,
        widget_type="number",
    )


def schedule(status: str = "draft") -> ShiftSchedule:
    return ShiftSchedule(
        id=uuid.uuid4(),
        date_start=date(2026, 6, 1),
        date_end=date(2026, 6, 30),
        status=status,
    )


def shift(schedule_id: uuid.UUID, employee_id: uuid.UUID) -> ScheduledShift:
    return ScheduledShift(
        id=uuid.uuid4(),
        shift_schedule_id=schedule_id,
        business_date=date(2026, 6, 10),
        employee_id=employee_id,
        payroll_role="pizza",
        station_code="Пицца",
        planned_start_at=datetime(2026, 6, 10, 10, tzinfo=UTC),
        planned_end_at=datetime(2026, 6, 10, 22, tzinfo=UTC),
    )


async def test_create_vacation_conflict_requires_confirmation() -> None:
    cook = employee()
    sched = schedule()
    scheduled = shift(sched.id, cook.id)
    session = VacationFakeSession(
        employees=[cook],
        schedules=[sched],
        shifts=[scheduled],
        settings=[vacation_setting(vacation_service.VACATION_DAYS_LIMIT_KEY, 20)],
    )

    with pytest.raises(VacationShiftConflictError) as exc_info:
        await vacation_service.create_vacation_period(
            session,  # type: ignore[arg-type]
            employee_id=cook.id,
            date_start=date(2026, 6, 10),
            date_end=date(2026, 6, 12),
            comment=None,
            actor=actor(),
        )

    assert exc_info.value.conflicts[0].shift_id == scheduled.id
    assert session.vacations == {}


async def test_create_vacation_force_removes_draft_shift() -> None:
    cook = employee()
    sched = schedule()
    scheduled = shift(sched.id, cook.id)
    session = VacationFakeSession(
        employees=[cook],
        schedules=[sched],
        shifts=[scheduled],
        settings=[vacation_setting(vacation_service.VACATION_DAYS_LIMIT_KEY, 20)],
    )

    period = await vacation_service.create_vacation_period(
        session,  # type: ignore[arg-type]
        employee_id=cook.id,
        date_start=date(2026, 6, 10),
        date_end=date(2026, 6, 12),
        comment=" отпуск ",
        actor=actor(),
        force_remove_conflicting_shifts=True,
    )

    assert period.days_count == 3
    assert period.comment == "отпуск"
    assert scheduled.id not in session.shifts
    assert session.agent_actions[0].action_type == "vacation_period_create"


async def test_vacation_limit_blocks_annual_overuse() -> None:
    cook = employee()
    existing = VacationPeriod(
        id=uuid.uuid4(),
        employee_id=cook.id,
        date_start=date(2026, 1, 1),
        date_end=date(2026, 1, 19),
        days_count=19,
        status="planned",
    )
    session = VacationFakeSession(
        employees=[cook],
        vacations=[existing],
        settings=[vacation_setting(vacation_service.VACATION_DAYS_LIMIT_KEY, 20)],
    )

    with pytest.raises(HTTPException) as exc_info:
        await vacation_service.create_vacation_period(
            session,  # type: ignore[arg-type]
            employee_id=cook.id,
            date_start=date(2026, 6, 10),
            date_end=date(2026, 6, 12),
            comment=None,
            actor=actor(),
        )

    assert exc_info.value.status_code == 422
    assert "Превышен годовой лимит" in exc_info.value.detail


async def test_vacation_period_cannot_be_longer_than_ten_days() -> None:
    cook = employee()
    session = VacationFakeSession(employees=[cook])

    with pytest.raises(HTTPException) as exc_info:
        await vacation_service.create_vacation_period(
            session,  # type: ignore[arg-type]
            employee_id=cook.id,
            date_start=date(2026, 6, 1),
            date_end=date(2026, 6, 11),
            comment=None,
            actor=actor(),
        )

    assert exc_info.value.status_code == 422
    assert "максимум на 10 дней" in exc_info.value.detail


async def test_second_vacation_requires_two_month_gap() -> None:
    cook = employee()
    existing = VacationPeriod(
        id=uuid.uuid4(),
        employee_id=cook.id,
        date_start=date(2026, 6, 1),
        date_end=date(2026, 6, 10),
        days_count=10,
        status="planned",
    )
    session = VacationFakeSession(employees=[cook], vacations=[existing])

    with pytest.raises(HTTPException) as exc_info:
        await vacation_service.create_vacation_period(
            session,  # type: ignore[arg-type]
            employee_id=cook.id,
            date_start=date(2026, 8, 9),
            date_end=date(2026, 8, 18),
            comment=None,
            actor=actor(),
        )

    assert exc_info.value.status_code == 422
    assert "не раньше 2026-08-10" in exc_info.value.detail


async def test_second_vacation_can_start_after_two_month_gap() -> None:
    cook = employee()
    existing = VacationPeriod(
        id=uuid.uuid4(),
        employee_id=cook.id,
        date_start=date(2026, 6, 1),
        date_end=date(2026, 6, 10),
        days_count=10,
        status="planned",
    )
    session = VacationFakeSession(employees=[cook], vacations=[existing])

    period = await vacation_service.create_vacation_period(
        session,  # type: ignore[arg-type]
        employee_id=cook.id,
        date_start=date(2026, 8, 10),
        date_end=date(2026, 8, 19),
        comment=None,
        actor=actor(),
    )

    assert period.days_count == 10
    assert period.date_start == date(2026, 8, 10)

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from app.models import CourierScheduleEntry, Employee
from app.services.couriers import schedule_service


class ScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def all(self) -> list[Any]:
        return self.rows


class ScheduleSession:
    def __init__(self, courier: Employee, actor: Employee) -> None:
        self.courier = courier
        self.actor = actor
        self.entries: dict[tuple[uuid.UUID, date], CourierScheduleEntry] = {}
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.flushed = 0

    async def get(self, model: Any, object_id: uuid.UUID) -> Any | None:
        if model is not Employee:
            return None
        if object_id == self.courier.id:
            return self.courier
        if object_id == self.actor.id:
            return self.actor
        return None

    def add(self, item: Any) -> None:
        self.added.append(item)
        if isinstance(item, CourierScheduleEntry):
            if item.id is None:
                item.id = len(self.entries) + 1
            self.entries[(item.courier_employee_id, item.work_date)] = item

    async def delete(self, item: Any) -> None:
        self.deleted.append(item)
        if isinstance(item, CourierScheduleEntry):
            self.entries.pop((item.courier_employee_id, item.work_date), None)

    async def flush(self) -> None:
        self.flushed += 1

    async def scalars(self, _query: Any) -> ScalarResult:
        return ScalarResult([])


def employee(position: str) -> Employee:
    return Employee(
        id=uuid.uuid4(),
        full_name=f"{position} Test",
        iiko_id=f"iiko-{uuid.uuid4()}",
        position=position,
        status="active",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        updated_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


async def test_upsert_entry_creates_then_updates_single_courier_day(monkeypatch) -> None:
    courier = employee("Курьер")
    actor = employee("Менеджер")
    session = ScheduleSession(courier, actor)
    recalculated: list[tuple[date, date, list[uuid.UUID]]] = []

    async def fake_get_entry(_session: Any, courier_id: uuid.UUID, work_date: date):
        return session.entries.get((courier_id, work_date))

    async def fake_recalculate(_session: Any, from_date: date, to_date: date, employee_ids=None):
        recalculated.append((from_date, to_date, list(employee_ids or [])))

    monkeypatch.setattr(schedule_service, "get_entry", fake_get_entry)
    monkeypatch.setattr(schedule_service, "recalculate_matches", fake_recalculate)

    work_date = date(2026, 6, 8)
    created = await schedule_service.upsert_entry(
        session,  # type: ignore[arg-type]
        courier_id=courier.id,
        work_date=work_date,
        category="primary",
        planned_start_at=None,
        planned_end_at=None,
        comment="утро",
        actor_id=actor.id,
    )
    updated = await schedule_service.upsert_entry(
        session,  # type: ignore[arg-type]
        courier_id=courier.id,
        work_date=work_date,
        category="secondary",
        planned_start_at=datetime(2026, 6, 8, 12, tzinfo=UTC),
        planned_end_at=datetime(2026, 6, 8, 20, tzinfo=UTC),
        comment="вечер",
        actor_id=actor.id,
    )

    assert created is updated
    assert len(session.entries) == 1
    assert updated.category == "secondary"
    assert updated.comment == "вечер"
    assert updated.planned_start_at.hour == 12
    assert recalculated == [
        (work_date, work_date, [courier.id]),
        (work_date, work_date, [courier.id]),
    ]


async def test_delete_entry_removes_shift_and_recalculates(monkeypatch) -> None:
    courier = employee("Курьер")
    actor = employee("Менеджер")
    session = ScheduleSession(courier, actor)
    work_date = date(2026, 6, 9)
    entry = CourierScheduleEntry(
        id=11,
        courier_employee_id=courier.id,
        work_date=work_date,
        category="primary",
        planned_start_at=datetime(2026, 6, 9, 10, tzinfo=UTC),
        planned_end_at=datetime(2026, 6, 9, 22, tzinfo=UTC),
        created_by=actor.id,
    )
    session.entries[(courier.id, work_date)] = entry
    cleared: list[int] = []
    recalculated: list[date] = []

    async def fake_get_entry(_session: Any, courier_id: uuid.UUID, target_date: date):
        return session.entries.get((courier_id, target_date))

    async def fake_clear(_session: Any, schedule_entry_id: int):
        cleared.append(schedule_entry_id)

    async def fake_recalculate(_session: Any, from_date: date, _to_date: date, employee_ids=None):
        recalculated.append(from_date)

    monkeypatch.setattr(schedule_service, "get_entry", fake_get_entry)
    monkeypatch.setattr(schedule_service, "clear_matches_for_deleted_schedule_entry", fake_clear)
    monkeypatch.setattr(schedule_service, "recalculate_matches", fake_recalculate)

    await schedule_service.delete_entry(
        session,  # type: ignore[arg-type]
        courier_id=courier.id,
        work_date=work_date,
        actor_id=actor.id,
    )

    assert session.entries == {}
    assert session.deleted == [entry]
    assert cleared == [11]
    assert recalculated == [work_date]


async def test_get_matrix_returns_window_entries(monkeypatch) -> None:
    entries = [
        CourierScheduleEntry(
            id=1,
            courier_employee_id=uuid.uuid4(),
            work_date=date(2026, 6, 10),
            category="primary",
            planned_start_at=datetime(2026, 6, 10, 10, tzinfo=UTC),
            planned_end_at=datetime(2026, 6, 10, 22, tzinfo=UTC),
            created_by=uuid.uuid4(),
        )
    ]

    async def fake_list_entries(_session: Any, filters: schedule_service.ScheduleFilters):
        assert filters.date_from == date(2026, 6, 10)
        assert filters.date_to == date(2026, 6, 16)
        return entries

    monkeypatch.setattr(schedule_service, "list_entries", fake_list_entries)

    matrix = await schedule_service.get_matrix(
        object(),  # type: ignore[arg-type]
        from_date=date(2026, 6, 10),
        to_date=date(2026, 6, 16),
    )

    assert matrix == entries

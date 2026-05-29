from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.db.session import get_session
from app.main import create_app
from app.models import (
    AgentAction,
    AgentRun,
    AttendanceEntry,
    DeliveryOrder,
    Employee,
    ShiftLedgerEntry,
)
from app.services.courier_sync import (
    parse_courier_delivery_rows,
    parse_iiko_olap_payload,
    sync_courier_deliveries,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures/iiko"


class FakeScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class FakeExecuteResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class FakeSession:
    def __init__(
        self,
        *,
        delivery_orders: list[DeliveryOrder] | None = None,
        employees: list[Employee] | None = None,
        ledger_entries: list[ShiftLedgerEntry] | None = None,
        attendance_entries: list[AttendanceEntry] | None = None,
    ) -> None:
        self.delivery_orders = delivery_orders or []
        self.employees = employees or []
        self.ledger_entries = ledger_entries or []
        self.attendance_entries = attendance_entries or []
        self.added: list[Any] = []
        self.commits = 0

    def add(self, item: Any) -> None:
        self.added.append(item)
        if isinstance(item, DeliveryOrder) and item not in self.delivery_orders:
            self.delivery_orders.append(item)

    async def flush(self) -> None:
        for item in self.added:
            if getattr(item, "id", None) is None:
                item.id = uuid.uuid4()

    async def commit(self) -> None:
        self.commits += 1

    async def scalar(self, _query: Any) -> Any | None:
        return None

    async def scalars(self, query: Any) -> FakeScalarResult:
        sql = compile_sql(query)
        if "delivery_order" not in sql:
            return FakeScalarResult([])
        rows = list(self.delivery_orders)
        date_from, date_to = date_bounds_from_query(query)
        if date_from is not None:
            rows = [row for row in rows if row.work_date >= date_from]
        if date_to is not None:
            rows = [row for row in rows if row.work_date <= date_to]
        courier_iiko_id = string_filter_from_query(query)
        if courier_iiko_id is not None and "courier_iiko_id" in sql:
            rows = [row for row in rows if row.courier_iiko_id == courier_iiko_id]
        rows.sort(key=lambda row: (row.work_date, row.opened_at or datetime.min, row.iiko_order_id))
        return FakeScalarResult(rows)

    async def execute(self, query: Any) -> FakeExecuteResult:
        sql = compile_sql(query)
        date_from, date_to = date_bounds_from_query(query)
        employee_id = uuid_filter_from_query(query)
        if "shift_ledger_entry" in sql:
            rows = [
                (entry, employee)
                for entry in self.ledger_entries
                if (employee := self.employee_by_id(entry.employee_id)) is not None
                and employee.position == "Курьер"
                and (date_from is None or entry.work_date >= date_from)
                and (date_to is None or entry.work_date <= date_to)
                and (employee_id is None or entry.employee_id == employee_id)
            ]
            return FakeExecuteResult(rows)
        if "attendance_entry" in sql:
            rows = [
                (entry, employee)
                for entry in self.attendance_entries
                if (employee := self.employee_by_id(entry.employee_id)) is not None
                and employee.position == "Курьер"
                and (date_from is None or entry.work_date >= date_from)
                and (date_to is None or entry.work_date <= date_to)
                and (employee_id is None or entry.employee_id == employee_id)
            ]
            return FakeExecuteResult(rows)
        return FakeExecuteResult([])

    def employee_by_id(self, employee_id: uuid.UUID) -> Employee | None:
        return next((employee for employee in self.employees if employee.id == employee_id), None)


def compile_sql(query: Any) -> str:
    return str(query.compile(compile_kwargs={"literal_binds": True}))


def date_bounds_from_query(query: Any) -> tuple[date | None, date | None]:
    values = [value for value in query.compile().params.values() if isinstance(value, date)]
    if len(values) < 2:
        return None, None
    return values[0], values[1]


def string_filter_from_query(query: Any) -> str | None:
    values = [value for value in query.compile().params.values() if isinstance(value, str)]
    return values[0] if values else None


def uuid_filter_from_query(query: Any) -> uuid.UUID | None:
    values = [value for value in query.compile().params.values() if isinstance(value, uuid.UUID)]
    return values[0] if values else None


def sample_rows() -> list[Any]:
    return parse_iiko_olap_payload((FIXTURE_DIR / "delivery_olap_sample.xml").read_bytes())


def test_delivery_olap_parser_reads_courier_order_facts() -> None:
    parsed = parse_courier_delivery_rows(sample_rows())

    assert parsed.skipped == 2
    assert parsed.errors == []
    assert [record.iiko_order_id for record in parsed.records] == [
        "order-onway",
        "order-delivered",
        "order-cancelled",
    ]
    assert parsed.records[0].work_date == date(2026, 5, 28)
    assert parsed.records[0].status == "OnWay"
    assert parsed.records[1].way_duration_minutes is not None
    assert str(parsed.records[1].way_duration_minutes) == "35.00"
    assert parsed.records[2].status == "Cancelled"


async def test_courier_sync_is_idempotent_for_same_olap_sample() -> None:
    session = FakeSession()

    first = await sync_courier_deliveries(
        session,
        date_from=date(2026, 5, 28),
        date_to=date(2026, 5, 29),
        run_reason="manual",
        iiko_rows=sample_rows(),
    )
    second = await sync_courier_deliveries(
        session,
        date_from=date(2026, 5, 28),
        date_to=date(2026, 5, 29),
        run_reason="manual",
        iiko_rows=sample_rows(),
    )

    assert first.as_dict() == {"inserted": 3, "updated": 0, "skipped": 2, "errors": 0}
    assert second.as_dict() == {"inserted": 0, "updated": 3, "skipped": 2, "errors": 0}
    assert len(session.delivery_orders) == 3
    assert len([item for item in session.added if isinstance(item, AgentRun)]) == 2
    assert len([item for item in session.added if isinstance(item, AgentAction)]) >= 8


async def test_courier_sync_updates_order_status_without_duplicate() -> None:
    session = FakeSession()
    first_row = {
        "Department": "Foodmarket Тепло Черникова",
        "Delivery.ServiceType": "COURIER",
        "UniqOrderId.Id": "order-status",
        "OrderNum": "1101",
        "OpenDate.Typed": "2026-05-28",
        "OpenTime": "2026-05-28T15:00:00+03:00",
        "Delivery.SendTime": "2026-05-28T15:10:00+03:00",
        "Delivery.Status": "OnWay",
        "Delivery.Courier.Id": "courier-1",
        "DishDiscountSumInt": "1200",
    }
    second_row = {
        **first_row,
        "Delivery.ActualTime": "2026-05-28T15:45:00+03:00",
        "Delivery.Status": "Delivered",
    }

    await sync_courier_deliveries(
        session,
        date_from=date(2026, 5, 28),
        date_to=date(2026, 5, 28),
        run_reason="manual",
        iiko_rows=[first_row],
    )
    result = await sync_courier_deliveries(
        session,
        date_from=date(2026, 5, 28),
        date_to=date(2026, 5, 28),
        run_reason="manual",
        iiko_rows=[second_row],
    )

    assert result.updated == 1
    assert len(session.delivery_orders) == 1
    assert session.delivery_orders[0].status == "Delivered"
    assert session.delivery_orders[0].closed_at == datetime(2026, 5, 28, 12, 45, tzinfo=UTC)


def test_get_deliveries_filters_by_date_and_courier_iiko_id() -> None:
    matching = make_delivery_order(
        iiko_order_id="order-1",
        work_date=date(2026, 5, 28),
        courier_iiko_id="courier-1",
    )
    wrong_courier = make_delivery_order(
        iiko_order_id="order-2",
        work_date=date(2026, 5, 28),
        courier_iiko_id="courier-2",
    )
    wrong_date = make_delivery_order(
        iiko_order_id="order-3",
        work_date=date(2026, 5, 27),
        courier_iiko_id="courier-1",
    )
    session = FakeSession(delivery_orders=[matching, wrong_courier, wrong_date])

    with TestClient(app_with_session(session)) as client:
        response = client.get(
            "/api/v1/couriers/deliveries",
            params={
                "date_from": "2026-05-28",
                "date_to": "2026-05-28",
                "courier_iiko_id": "courier-1",
            },
        )

    assert response.status_code == 200
    assert [item["iiko_order_id"] for item in response.json()] == ["order-1"]


def test_get_shifts_returns_courier_shifts_for_period() -> None:
    courier = make_employee(position="Курьер", full_name="Курьер Тестовый", iiko_id="courier-1")
    cook = make_employee(position="Повар", full_name="Повар Тестовый", iiko_id="cook-1")
    courier_shift = make_ledger_entry(courier.id, date(2026, 5, 28), 10)
    cook_shift = make_ledger_entry(cook.id, date(2026, 5, 28), 11)
    session = FakeSession(
        employees=[courier, cook],
        ledger_entries=[courier_shift, cook_shift],
    )

    with TestClient(app_with_session(session)) as client:
        response = client.get(
            "/api/v1/couriers/shifts",
            params={"date_from": "2026-05-28", "date_to": "2026-05-28"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["employee_id"] == str(courier.id)
    assert payload[0]["employee_iiko_id"] == "courier-1"
    assert payload[0]["source_table"] == "shift_ledger_entry"


def app_with_session(session: FakeSession):
    app = create_app()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    return app


def make_delivery_order(
    *,
    iiko_order_id: str,
    work_date: date,
    courier_iiko_id: str,
) -> DeliveryOrder:
    now = datetime(2026, 5, 29, 9, 0, tzinfo=UTC)
    return DeliveryOrder(
        id=uuid.uuid4(),
        iiko_order_id=iiko_order_id,
        order_number=re.sub(r"\D", "", iiko_order_id) or iiko_order_id,
        work_date=work_date,
        status="Delivered",
        service_type="COURIER",
        courier_iiko_id=courier_iiko_id,
        opened_at=datetime(2026, 5, 28, 7, 0, tzinfo=UTC),
        on_way_at=datetime(2026, 5, 28, 7, 10, tzinfo=UTC),
        closed_at=datetime(2026, 5, 28, 7, 40, tzinfo=UTC),
        revenue=1000,
        raw={},
        created_at=now,
        updated_at=now,
    )


def make_employee(*, position: str, full_name: str, iiko_id: str) -> Employee:
    return Employee(
        id=uuid.uuid4(),
        iiko_id=iiko_id,
        full_name=full_name,
        status="active",
        position=position,
        category=None,
        default_cooking_station=None,
        is_senior=False,
        is_deputy_senior=False,
    )


def make_ledger_entry(employee_id: uuid.UUID, work_date: date, hour: int) -> ShiftLedgerEntry:
    return ShiftLedgerEntry(
        id=uuid.uuid4(),
        work_date=work_date,
        employee_id=employee_id,
        payroll_role=None,
        category=None,
        source="fallback_primary",
        opened_at=datetime(2026, 5, 28, hour, 0, tzinfo=UTC),
        closed_at=datetime(2026, 5, 28, hour + 8, 0, tzinfo=UTC),
        notes=None,
        is_resolved=False,
    )

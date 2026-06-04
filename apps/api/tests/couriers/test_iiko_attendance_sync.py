from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from app.models import AgentAction, AgentRun, CourierIikoShift, Employee
from app.services.couriers.iiko_attendance_sync import (
    parse_attendance_xml,
    sync_attendance,
)


class FakeScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class AttendanceSyncSession:
    def __init__(self, employees: list[Employee]) -> None:
        self.employees = employees
        self.shifts: list[CourierIikoShift] = []
        self.added: list[Any] = []
        self.commits = 0
        self._next_shift_id = 1

    def add(self, item: Any) -> None:
        self.added.append(item)
        if isinstance(item, CourierIikoShift) and item not in self.shifts:
            self.shifts.append(item)

    async def flush(self) -> None:
        for item in self.added:
            if isinstance(item, (AgentRun, AgentAction)) and item.id is None:
                item.id = uuid.uuid4()
            elif isinstance(item, CourierIikoShift) and item.id is None:
                item.id = self._next_shift_id
                self._next_shift_id += 1

    async def commit(self) -> None:
        self.commits += 1

    async def scalar(self, query: Any) -> CourierIikoShift | None:
        params = query.compile().params
        iiko_employee_id = next(
            value for value in params.values() if isinstance(value, str)
        )
        opened_at = next(
            value for value in params.values() if isinstance(value, datetime)
        )
        return next(
            (
                shift
                for shift in self.shifts
                if shift.iiko_employee_id == iiko_employee_id and shift.opened_at == opened_at
            ),
            None,
        )

    async def scalars(self, query: Any) -> FakeScalarResult:
        params = query.compile().params
        iiko_ids = set()
        for value in params.values():
            if isinstance(value, str):
                iiko_ids.add(value)
            elif isinstance(value, (list, tuple, set)):
                iiko_ids.update(str(item) for item in value)
        return FakeScalarResult(
            [employee for employee in self.employees if employee.iiko_id in iiko_ids]
        )


async def test_iiko_attendance_sync_filters_role_and_is_idempotent() -> None:
    courier = make_employee("courier-1")
    session = AttendanceSyncSession([courier])

    first = await sync_attendance(
        session,
        from_date=date(2026, 5, 29),
        to_date=date(2026, 5, 29),
        attendance_xml=attendance_xml(),
        courier_role_id="courier-role",
        recalculate=False,
    )
    second = await sync_attendance(
        session,
        from_date=date(2026, 5, 29),
        to_date=date(2026, 5, 29),
        attendance_xml=attendance_xml(),
        courier_role_id="courier-role",
        recalculate=False,
    )

    assert first.as_dict() == {
        "total_records": 3,
        "matched_couriers": 2,
        "new": 2,
        "updated": 0,
        "unresolved_employees": 1,
        "errors": [],
    }
    assert second.new == 0
    assert second.updated == 2
    assert len(session.shifts) == 2
    assert {shift.iiko_employee_id for shift in session.shifts} == {
        "courier-1",
        "courier-missing",
    }
    assert session.shifts[0].employee_id == courier.id
    assert session.shifts[0].closed_at == datetime(2026, 5, 29, 12, 0, tzinfo=UTC)
    assert session.shifts[1].employee_id is None
    assert session.shifts[1].closed_at is None
    assert "name" not in session.shifts[0].raw_payload


def test_parse_attendance_xml_skips_bad_rows() -> None:
    parsed = parse_attendance_xml(
        """
        <attendances>
          <attendance>
            <employeeId>courier-1</employeeId>
            <roleId>courier-role</roleId>
            <dateFrom>2026-05-29T11:05:00+03:00</dateFrom>
            <attendanceType>P</attendanceType>
          </attendance>
          <attendance>
            <employeeId>bad</employeeId>
            <roleId>courier-role</roleId>
            <attendanceType>P</attendanceType>
          </attendance>
        </attendances>
        """
    )

    assert len(parsed.records) == 1
    assert len(parsed.errors) == 1
    assert parsed.records[0].opened_at == datetime(2026, 5, 29, 8, 5, tzinfo=UTC)


def attendance_xml() -> str:
    return """
    <attendances>
      <attendance>
        <employeeId>courier-1</employeeId>
        <roleId>courier-role</roleId>
        <dateFrom>2026-05-29T11:05:00+03:00</dateFrom>
        <dateTo>2026-05-29T15:00:00+03:00</dateTo>
        <attendanceType>P</attendanceType>
      </attendance>
      <attendance>
        <employeeId>cook-1</employeeId>
        <roleId>cook-role</roleId>
        <dateFrom>2026-05-29T10:00:00+03:00</dateFrom>
        <dateTo>2026-05-29T18:00:00+03:00</dateTo>
        <attendanceType>P</attendanceType>
      </attendance>
      <attendance>
        <employeeId>courier-missing</employeeId>
        <roleId>courier-role</roleId>
        <dateFrom>2026-05-29T16:00:00+03:00</dateFrom>
        <attendanceType>P</attendanceType>
      </attendance>
    </attendances>
    """


def make_employee(iiko_id: str) -> Employee:
    return Employee(
        id=uuid.uuid4(),
        full_name=f"{iiko_id} Test",
        iiko_id=iiko_id,
        position="Курьер",
        status="active",
    )

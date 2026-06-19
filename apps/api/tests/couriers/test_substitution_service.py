from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi import HTTPException

from app.models import CourierShiftSubstitution, Employee
from app.services.couriers import shift_day_service

WORK_DATE = date(2026, 6, 11)
ACTOR_ID = uuid.uuid4()


class SubstitutionSession:
    """Лёгкая in-memory сессия для проверки бизнес-правил подмены."""

    def __init__(
        self,
        *,
        employees: list[Employee] | None = None,
        existing: CourierShiftSubstitution | None = None,
    ) -> None:
        self.employees = {employee.id: employee for employee in employees or []}
        self.existing = existing
        self.added: list[Any] = []
        self.deleted: list[Any] = []

    async def get(self, model: Any, object_id: Any) -> Any | None:
        if model is Employee:
            return self.employees.get(object_id)
        return None

    async def scalar(self, _query: Any) -> Any | None:
        return self.existing

    def add(self, item: Any) -> None:
        self.added.append(item)

    async def delete(self, item: Any) -> None:
        self.deleted.append(item)

    async def flush(self) -> None:
        return None


def courier(*, placeholder: bool = False, position: str = "Курьер") -> Employee:
    return Employee(
        id=uuid.uuid4(),
        full_name="Курьер Test",
        iiko_id=f"iiko-{uuid.uuid4()}",
        position=position,
        status="active",
        is_courier_placeholder=placeholder,
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        updated_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


async def test_set_substitution_rejects_non_placeholder_card() -> None:
    placeholder = courier(placeholder=False)
    real = courier()
    session = SubstitutionSession(employees=[placeholder, real])
    with pytest.raises(HTTPException) as exc:
        await shift_day_service.set_substitution(
            session, WORK_DATE, placeholder.id, real.id, ACTOR_ID
        )
    assert exc.value.status_code == 400


async def test_set_substitution_rejects_real_being_placeholder() -> None:
    placeholder = courier(placeholder=True)
    real = courier(placeholder=True)
    session = SubstitutionSession(employees=[placeholder, real])
    with pytest.raises(HTTPException) as exc:
        await shift_day_service.set_substitution(
            session, WORK_DATE, placeholder.id, real.id, ACTOR_ID
        )
    assert exc.value.status_code == 400


async def test_set_substitution_rejects_non_courier_real() -> None:
    placeholder = courier(placeholder=True)
    real = courier(position="Кассир")
    session = SubstitutionSession(employees=[placeholder, real])
    with pytest.raises(HTTPException) as exc:
        await shift_day_service.set_substitution(
            session, WORK_DATE, placeholder.id, real.id, ACTOR_ID
        )
    assert exc.value.status_code == 400


async def test_set_substitution_creates_row() -> None:
    placeholder = courier(placeholder=True)
    real = courier()
    session = SubstitutionSession(employees=[placeholder, real])
    result = await shift_day_service.set_substitution(
        session, WORK_DATE, placeholder.id, real.id, ACTOR_ID
    )
    assert isinstance(result, CourierShiftSubstitution)
    assert result.real_employee_id == real.id
    assert result.placeholder_employee_id == placeholder.id
    assert result.created_by == ACTOR_ID
    assert len(session.added) == 1


async def test_set_substitution_updates_existing_row() -> None:
    placeholder = courier(placeholder=True)
    real = courier()
    existing = CourierShiftSubstitution(
        work_date=WORK_DATE,
        placeholder_employee_id=placeholder.id,
        real_employee_id=uuid.uuid4(),
    )
    session = SubstitutionSession(employees=[placeholder, real], existing=existing)
    result = await shift_day_service.set_substitution(
        session, WORK_DATE, placeholder.id, real.id, ACTOR_ID
    )
    assert result is existing
    assert result.real_employee_id == real.id
    assert session.added == []


async def test_clear_substitution_deletes_existing() -> None:
    existing = CourierShiftSubstitution(
        work_date=WORK_DATE,
        placeholder_employee_id=uuid.uuid4(),
        real_employee_id=uuid.uuid4(),
    )
    session = SubstitutionSession(existing=existing)
    await shift_day_service.clear_substitution(session, WORK_DATE, uuid.uuid4())
    assert session.deleted == [existing]


async def test_clear_substitution_noop_when_missing() -> None:
    session = SubstitutionSession(existing=None)
    await shift_day_service.clear_substitution(session, WORK_DATE, uuid.uuid4())
    assert session.deleted == []

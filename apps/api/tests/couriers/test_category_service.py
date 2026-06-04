from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from app.models import CourierCategory, CourierCategoryAssignment, Employee
from app.services.couriers import category_service


class CategoryAssignmentSession:
    def __init__(
        self,
        courier: Employee,
        actor: Employee,
        active_assignment: CourierCategoryAssignment,
    ) -> None:
        self.courier = courier
        self.actor = actor
        self.active_assignment = active_assignment
        self.scalar_calls = 0
        self.added: list[Any] = []
        self.flushed = False

    async def get(self, model: Any, object_id: uuid.UUID) -> Any | None:
        if model is not Employee:
            return None
        if object_id == self.courier.id:
            return self.courier
        if object_id == self.actor.id:
            return self.actor
        return None

    async def scalar(self, _query: Any) -> CourierCategoryAssignment | None:
        self.scalar_calls += 1
        if self.scalar_calls == 2:
            return self.active_assignment
        return None

    def add(self, item: Any) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        self.flushed = True


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


async def test_assign_category_closes_previous_period() -> None:
    courier = employee("Курьер")
    actor = employee("Менеджер")
    active = CourierCategoryAssignment(
        employee_id=courier.id,
        category=CourierCategory.PRIMARY,
        effective_from=date(2026, 6, 1),
        effective_to=None,
        created_by=actor.id,
    )
    session = CategoryAssignmentSession(courier, actor, active)

    assignment = await category_service.assign_category(
        session,
        employee_id=courier.id,
        category=CourierCategory.SECONDARY,
        effective_from=date(2026, 6, 10),
        actor_id=actor.id,
    )

    assert active.effective_to == date(2026, 6, 9)
    assert assignment.category == CourierCategory.SECONDARY
    assert assignment.effective_from == date(2026, 6, 10)
    assert assignment.effective_to is None
    assert session.added == [assignment]
    assert session.flushed is True

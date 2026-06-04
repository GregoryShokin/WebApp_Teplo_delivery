from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException

from app.models import (
    CourierEvaluation,
    CourierEvaluationCriterion,
    CourierEvaluationSource,
    Employee,
)
from app.services.couriers import evaluation_service


class EvaluationSession:
    def __init__(
        self,
        *,
        employees: list[Employee] | None = None,
        criteria: list[CourierEvaluationCriterion] | None = None,
        evaluations: list[CourierEvaluation] | None = None,
    ) -> None:
        self.employees = {employee.id: employee for employee in employees or []}
        self.criteria = {criterion.id: criterion for criterion in criteria or []}
        self.evaluations = {evaluation.id: evaluation for evaluation in evaluations or []}
        self.added: list[Any] = []
        self.next_evaluation_id = 1

    async def get(self, model: Any, object_id: Any) -> Any | None:
        if model is Employee:
            return self.employees.get(object_id)
        if model is CourierEvaluationCriterion:
            return self.criteria.get(object_id)
        if model is CourierEvaluation:
            return self.evaluations.get(object_id)
        return None

    async def scalar(self, _query: Any) -> Any | None:
        return None

    def add(self, item: Any) -> None:
        self.added.append(item)
        if isinstance(item, CourierEvaluation):
            item.id = self.next_evaluation_id
            self.next_evaluation_id += 1
            self.evaluations[item.id] = item

    async def flush(self) -> None:
        return None


def employee(position: str, *, admin: bool = False) -> Employee:
    item = Employee(
        id=uuid.uuid4(),
        full_name=f"{position} Test",
        iiko_id=f"iiko-{uuid.uuid4()}",
        position=position,
        status="active",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        updated_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    if admin:
        item.payroll_role = "admin"
    return item


def criterion(score: int = 7) -> CourierEvaluationCriterion:
    return CourierEvaluationCriterion(
        id=1,
        code="praise_from_client",
        label="Похвала от клиента",
        score=score,
        is_active=True,
        display_order=1,
    )


async def test_create_evaluation_requires_admin_cashier() -> None:
    actor = employee("Кассир", admin=False)
    courier = employee("Курьер")
    session = EvaluationSession(
        employees=[actor, courier],
        criteria=[criterion()],
    )

    with pytest.raises(HTTPException) as exc_info:
        await evaluation_service.create_evaluation(
            session,
            courier_id=courier.id,
            criterion_id=1,
            evaluated_at=date.today(),
            comment=None,
            actor_id=actor.id,
            source=CourierEvaluationSource.WEB,
        )

    assert exc_info.value.status_code == 403
    assert session.added == []


async def test_create_evaluation_snapshots_score_and_allows_multiple_per_day() -> None:
    actor = employee("Кассир", admin=True)
    courier = employee("Курьер")
    session = EvaluationSession(
        employees=[actor, courier],
        criteria=[criterion(score=5)],
    )

    first = await evaluation_service.create_evaluation(
        session,
        courier_id=courier.id,
        criterion_id=1,
        evaluated_at=date.today(),
        comment="Быстро закрыл заказ",
        actor_id=actor.id,
        source=CourierEvaluationSource.WEB,
    )
    second = await evaluation_service.create_evaluation(
        session,
        courier_id=courier.id,
        criterion_id=1,
        evaluated_at=date.today(),
        comment="Еще один эпизод",
        actor_id=actor.id,
        source=CourierEvaluationSource.WEB,
    )

    assert first.score_snapshot == 5
    assert second.score_snapshot == 5
    assert first.evaluated_at == second.evaluated_at
    assert len(session.added) == 2


async def test_update_evaluation_is_blocked_after_30_minutes() -> None:
    actor = employee("Кассир", admin=True)
    now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    evaluation = CourierEvaluation(
        id=1,
        courier_employee_id=uuid.uuid4(),
        criterion_id=1,
        score_snapshot=7,
        comment=None,
        evaluated_at=date.today(),
        source=CourierEvaluationSource.WEB,
        created_by=actor.id,
        created_at=now - timedelta(minutes=31),
        updated_at=now - timedelta(minutes=31),
    )
    session = EvaluationSession(employees=[actor], evaluations=[evaluation])

    with pytest.raises(HTTPException) as exc_info:
        await evaluation_service.update_evaluation(
            session,
            evaluation_id=1,
            actor_id=actor.id,
            comment="Поздняя правка",
            now=now,
        )

    assert exc_info.value.status_code == 403
    assert evaluation.comment is None


async def test_update_evaluation_is_limited_to_author() -> None:
    author = employee("Кассир", admin=True)
    other_actor = employee("Кассир", admin=True)
    now = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)
    evaluation = CourierEvaluation(
        id=1,
        courier_employee_id=uuid.uuid4(),
        criterion_id=1,
        score_snapshot=7,
        comment=None,
        evaluated_at=date.today(),
        source=CourierEvaluationSource.WEB,
        created_by=author.id,
        created_at=now,
        updated_at=now,
    )
    session = EvaluationSession(employees=[author, other_actor], evaluations=[evaluation])

    with pytest.raises(HTTPException) as exc_info:
        await evaluation_service.update_evaluation(
            session,
            evaluation_id=1,
            actor_id=other_actor.id,
            comment="Чужая правка",
            now=now,
        )

    assert exc_info.value.status_code == 403
    assert evaluation.comment is None

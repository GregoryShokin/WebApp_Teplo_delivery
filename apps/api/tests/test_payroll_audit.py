from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentActor, get_current_actor
from app.models import (
    Employee,
    EmployeePositionAssignment,
    PayrollAdjustment,
    PayrollRunEvent,
    User,
)
from app.schemas.payroll_config import PayrollRateBase
from app.services.payroll_config import create_rate_version

PAYROLL_AUDIT_TEST_PERMISSIONS = frozenset(
    {
        "payroll.runs.finalize",
        "payroll.bonuses.add",
        "payroll.penalties.add",
    }
)


async def test_rate_change_creates_audit_event(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first_payload = PayrollRateBase(
        position_group="QA аудит ставок",
        category="category_1",
        station=None,
        rate_type="daily",
        amount=Decimal("2200.00"),
        is_active=True,
        effective_from=date(2026, 1, 1),
        effective_to=None,
    )
    second_payload = PayrollRateBase(
        position_group="QA аудит ставок",
        category="category_1",
        station=None,
        rate_type="daily",
        amount=Decimal("2500.00"),
        is_active=True,
        effective_from=date(2026, 6, 1),
        effective_to=None,
    )

    async with async_session_factory() as session:
        actor = await create_user(session, full_name="Payroll Rate Auditor")
        await create_rate_version(session, first_payload)
        second = await create_rate_version(session, second_payload, actor_user_id=actor.id)
        events = (
            await session.scalars(
                select(PayrollRunEvent).where(PayrollRunEvent.action == "rate.changed")
            )
        ).all()

    event = next(item for item in events if item.payload["rate_id"] == str(second.id))
    assert event.run_id is None
    assert event.period_id is None
    assert event.actor_user_id == actor.id
    assert event.payload["old_amount"] == "2200.00"
    assert event.payload["new_amount"] == "2500.00"
    assert event.payload["effective_from"] == "2026-06-01"


async def test_adjustment_update_and_delete_create_audit_events(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_user(session, full_name="Payroll Audit Manager")
        employee = await create_employee(session)
        adjustment = PayrollAdjustment(
            id=uuid.uuid4(),
            employee_id=employee.id,
            work_date=date(2026, 5, 20),
            type="bonus",
            custom_label="Ручная премия",
            amount=Decimal("1000.00"),
            comment="До",
            created_by_user_id=actor.id,
            created_by_label="finance_manager",
        )
        session.add(adjustment)
        await session.commit()
        adjustment_id = adjustment.id
        employee_id = employee.id

    client.app.dependency_overrides[get_current_actor] = lambda: audit_actor(actor.id)
    try:
        patch_response = client.patch(
            f"/api/v1/payroll/adjustments/{adjustment_id}",
            json={"amount": "1500.00", "comment": "После"},
        )
        delete_response = client.delete(f"/api/v1/payroll/adjustments/{adjustment_id}")
    finally:
        client.app.dependency_overrides.pop(get_current_actor, None)

    assert patch_response.status_code == 200
    assert delete_response.status_code == 204

    async with async_session_factory() as session:
        events = (
            await session.scalars(
                select(PayrollRunEvent).where(
                    PayrollRunEvent.action.in_(
                        ("adjustment.updated", "adjustment.deleted")
                    )
                )
            )
        ).all()

    by_action = {event.action: event for event in events}
    updated = by_action["adjustment.updated"]
    deleted = by_action["adjustment.deleted"]

    assert updated.actor_user_id == actor.id
    assert updated.payload["adjustment_id"] == str(adjustment_id)
    assert updated.payload["employee_id"] == str(employee_id)
    assert updated.payload["before"]["amount"] == "1000.00"
    assert updated.payload["after"]["amount"] == "1500.00"
    assert updated.payload["after"]["comment"] == "После"

    assert deleted.actor_user_id == actor.id
    assert deleted.payload["before"]["amount"] == "1500.00"
    assert deleted.payload["after"] is None


async def test_get_audit_events_returns_events_and_filters_by_action(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_user(session, full_name="Payroll Audit Viewer")
        session.add_all(
            [
                PayrollRunEvent(
                    id=uuid.uuid4(),
                    action="rate.changed",
                    actor_user_id=actor.id,
                    payload={"marker": "old"},
                    created_at=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
                ),
                PayrollRunEvent(
                    id=uuid.uuid4(),
                    action="adjustment.updated",
                    actor_user_id=actor.id,
                    payload={"marker": "new"},
                    created_at=datetime(2026, 6, 2, 10, 0, tzinfo=UTC),
                ),
            ]
        )
        await session.commit()

    client.app.dependency_overrides[get_current_actor] = lambda: audit_actor(actor.id)
    try:
        response = client.get("/api/v1/payroll/audit-events")
        filtered = client.get(
            "/api/v1/payroll/audit-events",
            params={"action": "rate.changed"},
        )
    finally:
        client.app.dependency_overrides.pop(get_current_actor, None)

    assert response.status_code == 200
    assert [item["action"] for item in response.json()] == [
        "adjustment.updated",
        "rate.changed",
    ]
    assert response.json()[0]["actor"] == "Payroll Audit Viewer"

    assert filtered.status_code == 200
    assert [item["action"] for item in filtered.json()] == ["rate.changed"]
    assert filtered.json()[0]["payload"] == {"marker": "old"}


def audit_actor(user_id: uuid.UUID) -> CurrentActor:
    return CurrentActor(
        roles=frozenset({"finance_manager"}),
        user_id=user_id,
        permissions=PAYROLL_AUDIT_TEST_PERMISSIONS,
        permissions_loaded=True,
    )


async def create_user(session: AsyncSession, *, full_name: str) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"payroll-audit-{uuid.uuid4()}@test.local",
        hashed_password="hashed",
        full_name=full_name,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    return user


async def create_employee(session: AsyncSession) -> Employee:
    employee = Employee(
        id=uuid.uuid4(),
        full_name="Payroll Audit Employee",
        iiko_id=f"iiko-{uuid.uuid4()}",
        category="category_2",
        status="active",
        is_senior=False,
        is_deputy_senior=False,
        pin_hash="hashed-pin",
        pin_set_at=datetime(2026, 5, 1, tzinfo=UTC),
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
        updated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    session.add(employee)
    await session.flush()
    session.add(
        EmployeePositionAssignment(
            id=uuid.uuid4(),
            employee_id=employee.id,
            position="Повар",
            effective_from=date(2026, 1, 1),
            effective_to=None,
        )
    )
    await session.commit()
    await session.refresh(employee)
    return employee

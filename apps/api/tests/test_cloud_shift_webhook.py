"""Вебхук iikoCloud об открытии/закрытии смены курьера (realtime).

Покрывает: открытие смены курьера создаёт courier_iiko_shift (open, помечена источником
cloud_webhook); событие не-курьера игнорируется; закрытие проставляет closed_at.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import CourierIikoShift, Employee, EmployeePositionAssignment
from app.services.position_registry import (
    courier_positions,
    refresh_position_registry,
    reset_position_registry_for_tests,
)

BASE = "/api/v1/webhooks/iiko"
COURIER_IIKO_ID = "courier-iiko-webhook-1"


async def _seed_courier(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        await refresh_position_registry(session)
        positions = courier_positions()
        assert positions, "курьерские должности засеяны миграциями"
        employee_id = uuid.uuid4()
        session.add(
            Employee(
                id=employee_id,
                full_name="Курьер Вебхук",
                iiko_id=COURIER_IIKO_ID,
                status="active",
            )
        )
        # position — column_property из employee_position_assignment (effective-dated),
        # поэтому курьерскую должность задаём назначением, а не атрибутом.
        session.add(
            EmployeePositionAssignment(
                id=uuid.uuid4(),
                employee_id=employee_id,
                position=positions[0],
                effective_from=date(2020, 1, 1),
                effective_to=None,
            )
        )
        await session.commit()


async def _shifts(
    factory: async_sessionmaker[AsyncSession], iiko_id: str
) -> list[CourierIikoShift]:
    async with factory() as session:
        return list(
            (
                await session.scalars(
                    select(CourierIikoShift).where(
                        CourierIikoShift.iiko_employee_id == iiko_id
                    )
                )
            ).all()
        )


async def _refresh_registry(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        await refresh_position_registry(session)


def _event(employee_id: str, *, open_dt: str, close_dt: str | None = None) -> list[dict]:
    return [
        {
            "eventType": "PersonalShift",
            "eventTime": "2026-06-28T12:35:01.000",
            "eventInfo": {
                "employeeId": employee_id,
                "openDateTime": open_dt,
                "closeDateTime": close_dt,
            },
        }
    ]


def test_webhook_opens_courier_shift(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    reset_position_registry_for_tests()
    try:
        asyncio.run(_seed_courier(async_session_factory))
        asyncio.run(_refresh_registry(async_session_factory))

        response = client.post(BASE, json=_event(COURIER_IIKO_ID, open_dt="2026-06-28T12:35:00"))
        assert response.status_code == 200
        assert response.json()["processed"] == 1

        shifts = asyncio.run(_shifts(async_session_factory, COURIER_IIKO_ID))
        assert len(shifts) == 1
        assert shifts[0].closed_at is None
        assert shifts[0].employee_id is not None
        assert shifts[0].raw_payload.get("_source") == "cloud_webhook"
    finally:
        reset_position_registry_for_tests()


def test_webhook_close_sets_closed_at(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    reset_position_registry_for_tests()
    try:
        asyncio.run(_seed_courier(async_session_factory))
        asyncio.run(_refresh_registry(async_session_factory))

        client.post(BASE, json=_event(COURIER_IIKO_ID, open_dt="2026-06-28T12:35:00"))
        response = client.post(
            BASE,
            json=_event(
                COURIER_IIKO_ID,
                open_dt="2026-06-28T12:35:00",
                close_dt="2026-06-28T20:00:00",
            ),
        )
        assert response.status_code == 200

        shifts = asyncio.run(_shifts(async_session_factory, COURIER_IIKO_ID))
        assert len(shifts) == 1  # дедуп по (iiko_employee_id, opened_at)
        assert shifts[0].closed_at is not None
    finally:
        reset_position_registry_for_tests()


def test_webhook_ignores_non_courier(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    reset_position_registry_for_tests()
    try:
        asyncio.run(_refresh_registry(async_session_factory))
        response = client.post(
            BASE, json=_event("unknown-employee-guid", open_dt="2026-06-28T10:00:00")
        )
        assert response.status_code == 200
        assert response.json()["processed"] == 0
        shifts = asyncio.run(_shifts(async_session_factory, "unknown-employee-guid"))
        assert shifts == []
    finally:
        reset_position_registry_for_tests()


def test_webhook_skips_order_events(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Не-сменные события (заказы KDS) на общий /iiko просто пропускаются, не падают."""
    response = client.post(
        BASE,
        json=[
            {
                "eventType": "DeliveryOrderUpdate",
                "eventInfo": {"id": "order-guid", "order": {"id": "order-guid"}},
            }
        ],
    )
    assert response.status_code == 200
    body = response.json()
    assert body["processed"] == 0
    assert body["skipped"] == 1

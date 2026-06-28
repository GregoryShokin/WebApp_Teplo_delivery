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
from app.services.couriers.iiko_attendance_sync import sync_attendance
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


def _event(employee_id: str, *, opened: bool) -> list[dict]:
    # Реальная структура iikoCloud PersonalShift: сотрудник в `id`, флаг `opened`,
    # времени в eventInfo нет (время события — eventTime на верхнем уровне).
    return [
        {
            "eventType": "PersonalShift",
            "eventTime": "2026-06-28T12:35:01.000",
            "eventInfo": {
                "id": employee_id,
                "roleId": "courier-role",
                "opened": opened,
                "terminalGroupId": "tg-1",
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

        response = client.post(BASE, json=_event(COURIER_IIKO_ID, opened=True))
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

        client.post(BASE, json=_event(COURIER_IIKO_ID, opened=True))
        response = client.post(BASE, json=_event(COURIER_IIKO_ID, opened=False))
        assert response.status_code == 200

        shifts = asyncio.run(_shifts(async_session_factory, COURIER_IIKO_ID))
        assert len(shifts) == 1  # закрытие обновляет открытую смену, не плодит
        assert shifts[0].closed_at is not None
    finally:
        reset_position_registry_for_tests()


def test_webhook_close_without_open_is_noop(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Закрытие без открытой смены ничего не создаёт (поллинг доберёт)."""
    reset_position_registry_for_tests()
    try:
        asyncio.run(_seed_courier(async_session_factory))
        asyncio.run(_refresh_registry(async_session_factory))

        response = client.post(BASE, json=_event(COURIER_IIKO_ID, opened=False))
        assert response.status_code == 200
        assert response.json()["processed"] == 0
        shifts = asyncio.run(_shifts(async_session_factory, COURIER_IIKO_ID))
        assert shifts == []
    finally:
        reset_position_registry_for_tests()


def test_webhook_ignores_non_courier(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    reset_position_registry_for_tests()
    try:
        asyncio.run(_refresh_registry(async_session_factory))
        response = client.post(BASE, json=_event("unknown-employee-guid", opened=True))
        assert response.status_code == 200
        assert response.json()["processed"] == 0
        shifts = asyncio.run(_shifts(async_session_factory, "unknown-employee-guid"))
        assert shifts == []
    finally:
        reset_position_registry_for_tests()


async def _run_attendance_sync(
    factory: async_sessionmaker[AsyncSession], xml: str
) -> None:
    async with factory() as session:
        await sync_attendance(
            session,
            from_date=date.today(),
            to_date=date.today(),
            attendance_xml=xml,
            courier_role_ids={"courier-role"},
            recalculate=False,
        )


def test_polling_adopts_webhook_shift(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Поллинг усыновляет вебхук-смену (точное время), не плодит дубль за день."""
    reset_position_registry_for_tests()
    try:
        asyncio.run(_seed_courier(async_session_factory))
        asyncio.run(_refresh_registry(async_session_factory))

        # 1) вебхук открыл смену (opened_at ≈ now, помечена cloud_webhook)
        client.post(BASE, json=_event(COURIER_IIKO_ID, opened=True))
        # 2) поллинг приносит ту же смену с ТОЧНЫМ временем (другой opened_at), тот же день
        today = date.today().isoformat()
        xml = (
            "<attendances><attendance>"
            f"<employeeId>{COURIER_IIKO_ID}</employeeId>"
            "<roleId>courier-role</roleId>"
            f"<dateFrom>{today}T08:00:00+03:00</dateFrom>"
            "<attendanceType>P</attendanceType>"
            "</attendance></attendances>"
        )
        asyncio.run(_run_attendance_sync(async_session_factory, xml))

        shifts = asyncio.run(_shifts(async_session_factory, COURIER_IIKO_ID))
        assert len(shifts) == 1  # усыновлена, НЕ дубль
        # поллинг — источник истины: метка cloud_webhook перезаписана его payload'ом
        assert shifts[0].raw_payload.get("_source") != "cloud_webhook"
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

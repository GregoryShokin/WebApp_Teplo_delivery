"""Реестр помещений: справочник филиалов и их привязка к iiko."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

sys.path.append(str(Path(__file__).parent / "counterparties"))

from cp_helpers import admin_headers  # noqa: E402

BASE = "/api/v1/locations"


def _admin(factory) -> dict[str, str]:
    return asyncio.run(admin_headers(factory))


def test_migration_links_active_location_to_iiko(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Действующая точка получает идентификаторы iiko прямо из миграции.

    До реестра они были зашиты константами в коде, поэтому поведение обмена с iiko
    обязано совпадать до и после переезда.
    """
    response = client.get(BASE, headers=_admin(async_session_factory))
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    linked = [item for item in items if item["iiko_linked"]]
    assert linked, "хотя бы одна действующая точка должна быть подключена к iiko"
    for item in linked:
        assert item["iiko_organization_id"]
        assert item["iiko_department_id"]


def test_location_without_iiko_ids_is_valid(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Арендованный склад в iiko не заведён, но помещением остаётся: аренду он несёт."""
    headers = _admin(async_session_factory)
    response = client.post(
        BASE,
        headers=headers,
        json={"name": "Склад на Заводской", "kind": "warehouse", "address": "Заводская, 1"},
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["kind"] == "warehouse"
    assert created["iiko_linked"] is False
    assert created["iiko_store_ids"] == []
    assert created["status"] == "active"


def test_duplicate_name_is_rejected(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    headers = _admin(async_session_factory)
    payload = {"name": "Точка-дубль", "kind": "point"}
    assert client.post(BASE, headers=headers, json=payload).status_code == 201
    duplicate = client.post(BASE, headers=headers, json=payload)
    assert duplicate.status_code == 409, duplicate.text


def test_store_ids_are_deduplicated_and_trimmed(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Складов на помещении несколько (кухня, бар), но задвоенный склад сложит остатки дважды."""
    headers = _admin(async_session_factory)
    response = client.post(
        BASE,
        headers=headers,
        json={
            "name": "Точка со складами",
            "kind": "point",
            "iiko_store_ids": [" store-a ", "store-a", "store-b", "  "],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["iiko_store_ids"] == ["store-a", "store-b"]


def test_closing_date_survives_only_for_closed_location(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Дата закрытия у работающей точки — противоречие, отчёты бы врали."""
    headers = _admin(async_session_factory)
    created = client.post(
        BASE, headers=headers, json={"name": "Точка на закрытие", "kind": "point"}
    ).json()

    closed = client.patch(
        f"{BASE}/{created['id']}",
        headers=headers,
        json={
            "name": "Точка на закрытие",
            "kind": "point",
            "status": "inactive",
            "closed_on": "2026-07-31",
        },
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["closed_on"] == "2026-07-31"

    reopened = client.patch(
        f"{BASE}/{created['id']}",
        headers=headers,
        json={
            "name": "Точка на закрытие",
            "kind": "point",
            "status": "active",
            "closed_on": "2026-07-31",
        },
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["closed_on"] is None


def _make_location(client: TestClient, headers: dict[str, str], name: str) -> str:
    response = client.post(BASE, headers=headers, json={"name": name, "kind": "point"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _make_landlord(async_session_factory: async_sessionmaker[AsyncSession], name: str) -> str:
    from cp_helpers import make_counterparty  # noqa: PLC0415

    async def seed() -> str:
        async with async_session_factory() as session:
            counterparty = await make_counterparty(session, name=name)
            await session.commit()
            return str(counterparty.id)

    return asyncio.run(seed())


def test_lease_marks_counterparty_as_landlord(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Роль арендодателя проставляется сама: владельцу не нужно помнить про справочник ролей."""
    headers = _admin(async_session_factory)
    location_id = _make_location(client, headers, "Точка с арендой")
    landlord_id = _make_landlord(async_session_factory, "Арендодатель Иванов")

    response = client.post(
        f"{BASE}/{location_id}/leases",
        headers=headers,
        json={
            "counterparty_id": landlord_id,
            "monthly_amount": 100000,
            "payment_day": 1,
            "documents_mode": "informal",
            "deposit_amount": 100000,
            "started_on": "2026-01-01",
        },
    )
    assert response.status_code == 201, response.text
    lease = response.json()
    assert lease["counterparty_name"] == "Арендодатель Иванов"
    assert lease["monthly_amount"] == 100000.0
    assert lease["deposit_amount"] == 100000.0
    assert lease["is_active"] is True

    by_counterparty = client.get(f"{BASE}/leases/by-counterparty/{landlord_id}", headers=headers)
    assert by_counterparty.status_code == 200, by_counterparty.text
    assert [item["location_name"] for item in by_counterparty.json()["items"]] == [
        "Точка с арендой"
    ]


def test_changing_landlord_keeps_history(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Смена собственника не переписывает прошлые месяцы на нового арендодателя."""
    headers = _admin(async_session_factory)
    location_id = _make_location(client, headers, "Точка со сменой собственника")
    first = _make_landlord(async_session_factory, "Прежний собственник")
    second = _make_landlord(async_session_factory, "Новый собственник")

    created = client.post(
        f"{BASE}/{location_id}/leases",
        headers=headers,
        json={
            "counterparty_id": first,
            "monthly_amount": 100000,
            "started_on": "2026-01-01",
        },
    ).json()

    closed = client.post(
        f"{BASE}/{location_id}/leases/{created['id']}/close",
        headers=headers,
        json={"ended_on": "2026-06-30"},
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["is_active"] is False

    client.post(
        f"{BASE}/{location_id}/leases",
        headers=headers,
        json={
            "counterparty_id": second,
            "monthly_amount": 110000,
            "documents_mode": "official",
            "started_on": "2026-07-01",
        },
    )

    leases = client.get(f"{BASE}/{location_id}/leases", headers=headers).json()["items"]
    assert len(leases) == 2
    active = [item for item in leases if item["is_active"]]
    assert len(active) == 1
    assert active[0]["counterparty_name"] == "Новый собственник"
    assert active[0]["monthly_amount"] == 110000.0
    history = [item for item in leases if not item["is_active"]]
    assert history[0]["counterparty_name"] == "Прежний собственник"
    assert history[0]["ended_on"] == "2026-06-30"


def test_two_landlords_can_share_one_location(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Площадь бывает поделена: зал и склад в одном адресе сдают разные лица."""
    headers = _admin(async_session_factory)
    location_id = _make_location(client, headers, "Точка на двух собственников")
    first = _make_landlord(async_session_factory, "Собственник зала")
    second = _make_landlord(async_session_factory, "Собственник склада")

    for landlord, amount in ((first, 70000), (second, 30000)):
        response = client.post(
            f"{BASE}/{location_id}/leases",
            headers=headers,
            json={
                "counterparty_id": landlord,
                "monthly_amount": amount,
                "started_on": "2026-01-01",
            },
        )
        assert response.status_code == 201, response.text

    leases = client.get(f"{BASE}/{location_id}/leases", headers=headers).json()["items"]
    active = [item for item in leases if item["is_active"]]
    assert len(active) == 2
    assert sum(item["monthly_amount"] for item in active) == 100000.0


def test_lease_period_order_is_validated(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    headers = _admin(async_session_factory)
    location_id = _make_location(client, headers, "Точка с кривыми датами")
    landlord = _make_landlord(async_session_factory, "Собственник с датами")

    response = client.post(
        f"{BASE}/{location_id}/leases",
        headers=headers,
        json={
            "counterparty_id": landlord,
            "monthly_amount": 50000,
            "started_on": "2026-07-01",
            "ended_on": "2026-06-01",
        },
    )
    assert response.status_code == 422, response.text

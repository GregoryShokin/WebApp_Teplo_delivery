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

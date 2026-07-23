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


def test_lease_creates_landlord_from_form(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Арендодателя заводим прямо из карточки помещения: в справочнике его ещё нет."""
    headers = _admin(async_session_factory)
    location_id = _make_location(client, headers, "Точка с новым арендодателем")

    response = client.post(
        f"{BASE}/{location_id}/leases",
        headers=headers,
        json={
            "landlord": {"name": "ИП Иванов И. И."},
            "monthly_amount": 100000,
            "payment_day": 1,
            "documents_mode": "informal",
            "deposit_amount": 100000,
            "started_on": "2026-01-01",
        },
    )
    assert response.status_code == 201, response.text
    lease = response.json()
    assert lease["counterparty_name"] == "ИП Иванов И. И."
    assert lease["monthly_amount"] == 100000.0
    assert lease["deposit_amount"] == 100000.0
    assert lease["is_active"] is True

    landlord_id = lease["counterparty_id"]
    by_counterparty = client.get(f"{BASE}/leases/by-counterparty/{landlord_id}", headers=headers)
    assert by_counterparty.status_code == 200, by_counterparty.text
    assert [item["location_name"] for item in by_counterparty.json()["items"]] == [
        "Точка с новым арендодателем"
    ]


def test_same_landlord_is_reused_across_locations(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Один собственник сдаёт и точку, и склад — карточка должна остаться одна.

    Регрессия: чтение уже существующей роли ``landlord`` падало LookupError, потому что
    значение добавили в тип PostgreSQL, но не в Python-описание enum. Первая аренда проходила,
    вторая на того же собственника — нет.
    """
    headers = _admin(async_session_factory)
    first_location = _make_location(client, headers, "Зал у общего собственника")
    second_location = _make_location(client, headers, "Склад у общего собственника")

    first = client.post(
        f"{BASE}/{first_location}/leases",
        headers=headers,
        json={
            "landlord": {"name": "ИП Общий Собственник"},
            "monthly_amount": 100000,
            "started_on": "2026-01-01",
        },
    )
    assert first.status_code == 201, first.text

    second = client.post(
        f"{BASE}/{second_location}/leases",
        headers=headers,
        # Регистр другой — для человека это тот же собственник, дубля быть не должно.
        json={
            "landlord": {"name": "ип общий собственник"},
            "monthly_amount": 10000,
            "started_on": "2026-02-01",
        },
    )
    assert second.status_code == 201, second.text
    assert second.json()["counterparty_id"] == first.json()["counterparty_id"]

    leases = client.get(
        f"{BASE}/leases/by-counterparty/{first.json()['counterparty_id']}", headers=headers
    ).json()["items"]
    assert sorted(item["location_name"] for item in leases) == [
        "Зал у общего собственника",
        "Склад у общего собственника",
    ]


def test_closing_lease_leaves_location_without_landlord(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """«Съехали» без замены: активных арендодателей нет, история остаётся.

    Смену собственника проверяет test_replacing_landlord_archives_the_previous_one — здесь
    именно случай, когда помещение освободилось и новый договор ещё не заключён.
    """
    headers = _admin(async_session_factory)
    location_id = _make_location(client, headers, "Точка, которую освободили")
    created = client.post(
        f"{BASE}/{location_id}/leases",
        headers=headers,
        json={
            "landlord": {"name": "Собственник до переезда"},
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

    leases = client.get(f"{BASE}/{location_id}/leases", headers=headers).json()["items"]
    assert len(leases) == 1
    assert [item["is_active"] for item in leases] == [False]
    assert leases[0]["counterparty_name"] == "Собственник до переезда"
    assert leases[0]["ended_on"] == "2026-06-30"


def test_two_landlords_can_share_one_location(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Площадь бывает поделена: зал и склад в одном адресе сдают разные лица."""
    headers = _admin(async_session_factory)
    location_id = _make_location(client, headers, "Точка на двух собственников")
    for landlord, amount in (("Собственник зала", 70000), ("Собственник склада", 30000)):
        response = client.post(
            f"{BASE}/{location_id}/leases",
            headers=headers,
            json={
                "landlord": {"name": landlord},
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
    response = client.post(
        f"{BASE}/{location_id}/leases",
        headers=headers,
        json={
            "landlord": {"name": "Собственник с датами"},
            "monthly_amount": 50000,
            "started_on": "2026-07-01",
            "ended_on": "2026-06-01",
        },
    )
    assert response.status_code == 422, response.text


def test_editing_terms_never_swaps_landlord(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Правка суммы — это договорённость с тем же собственником, а не его замена."""
    headers = _admin(async_session_factory)
    location_id = _make_location(client, headers, "Точка с правкой условий")
    created = client.post(
        f"{BASE}/{location_id}/leases",
        headers=headers,
        json={
            "landlord": {"name": "Собственник условий"},
            "monthly_amount": 100000,
            "started_on": "2026-01-01",
        },
    ).json()

    updated = client.patch(
        f"{BASE}/{location_id}/leases/{created['id']}",
        headers=headers,
        json={"monthly_amount": 120000, "started_on": "2026-01-01"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["counterparty_id"] == created["counterparty_id"]
    assert updated.json()["monthly_amount"] == 120000.0

    # Попытка подсунуть арендодателя в правку условий должна отлетать, а не менять его молча.
    sneaky = client.patch(
        f"{BASE}/{location_id}/leases/{created['id']}",
        headers=headers,
        json={
            "monthly_amount": 120000,
            "started_on": "2026-01-01",
            "landlord": {"name": "Подменённый собственник"},
        },
    )
    assert sneaky.status_code == 422, sneaky.text


def test_official_lease_requires_landlord_requisites(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Официальная аренда без реквизитов бессмысленна: платёж по УПД некуда отправить."""
    headers = _admin(async_session_factory)
    location_id = _make_location(client, headers, "Точка с официальной арендой")

    refused = client.post(
        f"{BASE}/{location_id}/leases",
        headers=headers,
        json={
            "landlord": {"name": "ООО Без Реквизитов"},
            "monthly_amount": 100000,
            "documents_mode": "official",
            "started_on": "2026-01-01",
        },
    )
    assert refused.status_code == 422, refused.text
    assert "реквизиты" in refused.json()["detail"].lower()

    accepted = client.post(
        f"{BASE}/{location_id}/leases",
        headers=headers,
        json={
            "landlord": {
                "name": "ООО С Реквизитами",
                "inn": "7707083893",
                "bank_bik": "044525225",
                "bank_account": "40702810900000000001",
                "corr_account": "30101810400000000225",
            },
            "monthly_amount": 100000,
            "documents_mode": "official",
            "started_on": "2026-01-01",
        },
    )
    assert accepted.status_code == 201, accepted.text


def test_replacing_landlord_archives_the_previous_one(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Смена собственника: прежняя аренда закрывается, прежний арендодатель уходит в архив."""
    headers = _admin(async_session_factory)
    location_id = _make_location(client, headers, "Точка со сменой владельца")
    created = client.post(
        f"{BASE}/{location_id}/leases",
        headers=headers,
        json={
            "landlord": {"name": "Уходящий собственник"},
            "monthly_amount": 100000,
            "started_on": "2026-01-01",
        },
    ).json()

    replaced = client.post(
        f"{BASE}/{location_id}/leases/{created['id']}/replace-landlord",
        headers=headers,
        json={
            "landlord": {"name": "Пришедший собственник"},
            "terms": {"monthly_amount": 130000, "started_on": "2026-07-01"},
            "previous_ended_on": "2026-06-30",
        },
    )
    assert replaced.status_code == 201, replaced.text
    body = replaced.json()
    assert body["previous"]["ended_on"] == "2026-06-30"
    assert body["previous"]["is_active"] is False
    assert body["current"]["counterparty_name"] == "Пришедший собственник"
    assert body["current"]["monthly_amount"] == 130000.0
    assert body["previous_archived"] is True

    leases = client.get(f"{BASE}/{location_id}/leases", headers=headers).json()["items"]
    assert [item["is_active"] for item in leases].count(True) == 1


def test_landlord_renting_another_location_is_not_archived(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Тот же собственник сдаёт вторую точку — в архив ему рано."""
    headers = _admin(async_session_factory)
    first = _make_location(client, headers, "Первая точка общего владельца")
    second = _make_location(client, headers, "Вторая точка общего владельца")
    landlord = {"name": "Собственник двух точек"}

    client.post(
        f"{BASE}/{second}/leases",
        headers=headers,
        json={"landlord": landlord, "monthly_amount": 10000, "started_on": "2026-01-01"},
    )
    created = client.post(
        f"{BASE}/{first}/leases",
        headers=headers,
        json={"landlord": landlord, "monthly_amount": 50000, "started_on": "2026-01-01"},
    ).json()

    replaced = client.post(
        f"{BASE}/{first}/leases/{created['id']}/replace-landlord",
        headers=headers,
        json={
            "landlord": {"name": "Сменщик на первой точке"},
            "terms": {"monthly_amount": 60000, "started_on": "2026-08-01"},
            "previous_ended_on": "2026-07-31",
        },
    )
    assert replaced.status_code == 201, replaced.text
    assert replaced.json()["previous_archived"] is False


def test_replacing_with_the_same_landlord_is_rejected(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """«Смена» на самого себя — это правка условий, а не новая строка аренды."""
    headers = _admin(async_session_factory)
    location_id = _make_location(client, headers, "Точка с мнимой сменой")
    created = client.post(
        f"{BASE}/{location_id}/leases",
        headers=headers,
        json={
            "landlord": {"name": "Тот же самый собственник"},
            "monthly_amount": 100000,
            "started_on": "2026-01-01",
        },
    ).json()

    response = client.post(
        f"{BASE}/{location_id}/leases/{created['id']}/replace-landlord",
        headers=headers,
        json={
            "landlord": {"name": "тот же самый собственник"},
            "terms": {"monthly_amount": 110000, "started_on": "2026-07-01"},
            "previous_ended_on": "2026-06-30",
        },
    )
    assert response.status_code == 409, response.text

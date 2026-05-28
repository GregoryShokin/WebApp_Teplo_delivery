from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import payroll_config as payroll_config_routes
from app.main import create_app


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    test_client = TestClient(app)
    yield test_client
    test_client.close()


def _rate(
    *,
    amount: float,
    effective_from: date,
    effective_to: date | None = None,
) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "position_group": "Пиццерист",
        "category": "category_2",
        "station": None,
        "rate_type": "daily",
        "amount": amount,
        "is_active": True,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "created_at": datetime(2026, 5, 27, 10, 0, tzinfo=UTC),
    }


def test_get_payroll_rates_current_returns_current_rows(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _rate(amount=2200, effective_from=date(2026, 1, 1))

    async def fake_list_rates(_session, *, history: bool = False):
        assert history is False
        return [current]

    monkeypatch.setattr(payroll_config_routes, "list_rates", fake_list_rates)

    response = client.get("/api/v1/payroll/config/rates", headers={"X-User-Role": "manager"})

    assert response.status_code == 200
    assert response.json()[0]["amount"] == 2200
    assert response.json()[0]["category"] == "category_2"


def test_get_payroll_rates_history_passes_history_flag(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _rate(
        amount=2200,
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 5, 1),
    )
    current = _rate(amount=2400, effective_from=date(2026, 5, 1))

    async def fake_list_rates(_session, *, history: bool = False):
        assert history is True
        return [old, current]

    monkeypatch.setattr(payroll_config_routes, "list_rates", fake_list_rates)

    response = client.get(
        "/api/v1/payroll/config/rates",
        params={"history": "true"},
        headers={"X-User-Role": "manager"},
    )

    assert response.status_code == 200
    assert [item["amount"] for item in response.json()] == [2200, 2400]


def test_put_payroll_rate_creates_new_version_and_preserves_old(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = [_rate(amount=2200, effective_from=date(2026, 1, 1))]

    async def fake_create_rate_version(_session, payload):
        store[0]["effective_to"] = payload.effective_from
        created = {
            "id": uuid.uuid4(),
            **payload.model_dump(),
            "created_at": datetime(2026, 5, 27, 11, 0, tzinfo=UTC),
        }
        store.append(created)
        return created

    monkeypatch.setattr(payroll_config_routes, "create_rate_version", fake_create_rate_version)

    response = client.put(
        "/api/v1/payroll/config/rates",
        headers={"X-User-Role": "finance_manager"},
        json={
            "position_group": "Пиццерист",
            "category": "category_2",
            "station": None,
            "rate_type": "daily",
            "amount": 2500,
            "is_active": True,
            "effective_from": "2026-06-01",
            "effective_to": None,
        },
    )

    assert response.status_code == 200
    assert response.json()["amount"] == 2500
    assert len(store) == 2
    assert store[0]["amount"] == 2200
    assert store[0]["effective_to"] == date(2026, 6, 1)


def test_put_payroll_rate_manager_returns_403(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_create_rate_version(*_args, **_kwargs):
        raise AssertionError("manager must not write payroll config")

    monkeypatch.setattr(payroll_config_routes, "create_rate_version", fake_create_rate_version)

    response = client.put(
        "/api/v1/payroll/config/rates",
        headers={"X-User-Role": "manager"},
        json={
            "position_group": "Пиццерист",
            "category": "category_2",
            "station": None,
            "rate_type": "daily",
            "amount": 2500,
            "is_active": True,
            "effective_from": "2026-06-01",
            "effective_to": None,
        },
    )

    assert response.status_code == 403


@pytest.mark.parametrize("method", ["post", "put"])
def test_write_payroll_rate_invalid_category_returns_400(
    client: TestClient,
    method: str,
) -> None:
    response = getattr(client, method)(
        "/api/v1/payroll/config/rates",
        headers={"X-User-Role": "finance_manager"},
        json={
            "position_group": "Пиццерист",
            "category": "category_6",
            "station": None,
            "rate_type": "daily",
            "amount": 2500,
            "is_active": True,
            "effective_from": "2026-06-01",
            "effective_to": None,
        },
    )

    assert response.status_code == 400

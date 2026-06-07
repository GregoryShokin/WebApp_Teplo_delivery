from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1 import payroll_config as payroll_config_routes
from app.main import create_app
from app.models import PayrollRate
from app.schemas.payroll_config import PayrollRateBase
from app.services.payroll_config import (
    PayrollConfigValidationError,
    create_rate_version,
)


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    test_client = TestClient(app)
    yield test_client
    test_client.close()


def _rate(
    *,
    amount: float | None,
    effective_from: date,
    effective_to: date | None = None,
    is_active: bool = True,
) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "position_group": "Пиццерист",
        "category": "category_2",
        "station": None,
        "rate_type": "daily",
        "amount": amount,
        "is_active": is_active,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "created_at": datetime(2026, 5, 27, 10, 0, tzinfo=UTC),
        "is_enabled": True,
    }


def _availability(
    *,
    position_group: str = "Пиццерист",
    category: str = "category_2",
    is_enabled: bool = True,
) -> dict[str, Any]:
    return {
        "position_group": position_group,
        "category": category,
        "is_enabled": is_enabled,
    }


def _tier(
    *,
    min_revenue: float,
    max_revenue: float | None,
    rate_percent: float,
    effective_from: date,
    effective_to: date | None = None,
) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "min_revenue": min_revenue,
        "max_revenue": max_revenue,
        "rate_percent": rate_percent,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "created_at": datetime(2026, 5, 28, 10, 0, tzinfo=UTC),
    }


def _coefficient(
    *,
    category: str,
    coefficient: float,
    effective_from: date,
    effective_to: date | None = None,
) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "category": category,
        "coefficient": coefficient,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "created_at": datetime(2026, 5, 28, 10, 0, tzinfo=UTC),
    }


def test_get_payroll_rates_current_returns_current_rows(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _rate(amount=2200, effective_from=date(2026, 1, 1))

    async def fake_list_rate_matrix(_session, *, include_disabled: bool = False):
        assert include_disabled is False
        return [current]

    monkeypatch.setattr(payroll_config_routes, "list_rate_matrix", fake_list_rate_matrix)

    response = client.get("/api/v1/payroll/config/rates", headers={"X-User-Role": "manager"})

    assert response.status_code == 200
    assert response.json()[0]["amount"] == 2200
    assert response.json()[0]["category"] == "category_2"
    assert response.json()[0]["is_enabled"] is True


def test_get_payroll_rates_can_include_disabled_cells(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled = {
        "id": None,
        "position_group": "Шаурмист",
        "category": "category_1",
        "station": "shawarma",
        "rate_type": "daily",
        "amount": None,
        "is_active": True,
        "is_enabled": False,
        "effective_from": None,
        "effective_to": None,
        "created_at": None,
    }

    async def fake_list_rate_matrix(_session, *, include_disabled: bool = False):
        assert include_disabled is True
        return [disabled]

    monkeypatch.setattr(payroll_config_routes, "list_rate_matrix", fake_list_rate_matrix)

    response = client.get(
        "/api/v1/payroll/config/rates",
        params={"include_disabled": "true"},
        headers={"X-User-Role": "manager"},
    )

    assert response.status_code == 200
    assert response.json()[0]["id"] is None
    assert response.json()[0]["amount"] is None
    assert response.json()[0]["is_enabled"] is False


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


def test_put_payroll_rate_active_null_amount_returns_422(client: TestClient) -> None:
    response = client.put(
        "/api/v1/payroll/config/rates",
        headers={"X-User-Role": "finance_manager"},
        json={
            "position_group": "Шаурмист",
            "category": "category_1",
            "station": "shawarma",
            "rate_type": "daily",
            "amount": None,
            "is_active": True,
            "effective_from": "2026-06-01",
            "effective_to": None,
        },
    )

    assert response.status_code == 422
    assert "Активная ставка требует сумму > 0" in str(response.json()["detail"])


@pytest.mark.parametrize("amount", [0, -100])
def test_put_payroll_rate_active_non_positive_amount_returns_422(
    client: TestClient,
    amount: float,
) -> None:
    response = client.put(
        "/api/v1/payroll/config/rates",
        headers={"X-User-Role": "finance_manager"},
        json={
            "position_group": "Шаурмист",
            "category": "category_1",
            "station": "shawarma",
            "rate_type": "daily",
            "amount": amount,
            "is_active": True,
            "effective_from": "2026-06-01",
            "effective_to": None,
        },
    )

    assert response.status_code == 422
    assert "Активная ставка требует сумму > 0" in str(response.json()["detail"])


def test_put_payroll_rate_allows_inactive_null_amount(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_create_rate_version(_session, payload):
        return {
            "id": uuid.uuid4(),
            **payload.model_dump(),
            "created_at": datetime(2026, 5, 27, 11, 0, tzinfo=UTC),
        }

    monkeypatch.setattr(payroll_config_routes, "create_rate_version", fake_create_rate_version)

    response = client.put(
        "/api/v1/payroll/config/rates",
        headers={"X-User-Role": "finance_manager"},
        json={
            "position_group": "Шаурмист",
            "category": "category_1",
            "station": "shawarma",
            "rate_type": "daily",
            "amount": None,
            "is_active": False,
            "effective_from": "2026-06-01",
            "effective_to": None,
        },
    )

    assert response.status_code == 200
    assert response.json()["amount"] is None
    assert response.json()["is_active"] is False


@pytest.mark.parametrize("method", ["post", "put"])
def test_write_payroll_rate_invalid_category_returns_422(
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

    assert response.status_code == 422


@pytest.mark.parametrize("amount", [None, 0])
async def test_create_rate_version_active_without_positive_amount_raises(
    async_session_factory: async_sessionmaker[AsyncSession],
    amount: float | None,
) -> None:
    payload = PayrollRateBase.model_construct(
        position_group="QA ставка",
        category="category_1",
        station=None,
        rate_type="daily",
        amount=amount,
        is_active=True,
        effective_from=date(2026, 6, 1),
        effective_to=None,
    )

    async with async_session_factory() as session:
        with pytest.raises(PayrollConfigValidationError, match="Активная ставка требует сумму > 0"):
            await create_rate_version(session, payload)


async def test_create_rate_version_closes_previous_version_and_keeps_new_active(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first_payload = PayrollRateBase(
        position_group="QA ставка",
        category="category_1",
        station=None,
        rate_type="daily",
        amount=2200,
        is_active=True,
        effective_from=date(2026, 1, 1),
        effective_to=None,
    )
    second_payload = PayrollRateBase(
        position_group="QA ставка",
        category="category_1",
        station=None,
        rate_type="daily",
        amount=2500,
        is_active=True,
        effective_from=date(2026, 6, 1),
        effective_to=None,
    )

    async with async_session_factory() as session:
        first = await create_rate_version(session, first_payload)
        second = await create_rate_version(session, second_payload)

        rows = list(
            (
                await session.scalars(
                    select(PayrollRate)
                    .where(
                        PayrollRate.position_group == "QA ставка",
                        PayrollRate.category == "category_1",
                    )
                    .order_by(PayrollRate.effective_from)
                )
            ).all()
        )

    assert len(rows) == 2
    assert rows[0].id == first.id
    assert rows[0].effective_to == date(2026, 6, 1)
    assert rows[0].is_active is True
    assert rows[1].id == second.id
    assert rows[1].effective_from == date(2026, 6, 1)
    assert rows[1].effective_to is None
    assert rows[1].is_active is True
    assert rows[1].amount == 2500


async def test_payroll_rate_active_null_amount_is_rejected_by_db(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        session.add(
            PayrollRate(
                position_group="QA DB constraint",
                category="category_1",
                station=None,
                rate_type="daily",
                amount=None,
                is_active=True,
                effective_from=date(2026, 6, 1),
                effective_to=None,
            )
        )

        with pytest.raises(IntegrityError):
            await session.commit()

        await session.rollback()


def test_get_payroll_availability_returns_matrix(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matrix = [
        _availability(category="category_1", is_enabled=True),
        _availability(category="category_2", is_enabled=False),
    ]

    async def fake_list_role_category_availability(_session):
        return matrix

    monkeypatch.setattr(
        payroll_config_routes,
        "list_role_category_availability",
        fake_list_role_category_availability,
    )

    response = client.get(
        "/api/v1/payroll/config/availability",
        headers={"X-User-Role": "manager"},
    )

    assert response.status_code == 200
    assert response.json() == matrix


def test_put_payroll_availability_toggles_role_category(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_set_role_category_availability(
        _session,
        *,
        position_group: str,
        category: str,
        is_enabled: bool,
    ):
        seen.update(
            {
                "position_group": position_group,
                "category": category,
                "is_enabled": is_enabled,
            }
        )
        return seen

    monkeypatch.setattr(
        payroll_config_routes,
        "set_role_category_availability",
        fake_set_role_category_availability,
    )

    response = client.put(
        "/api/v1/payroll/config/availability/Шаурмист/category_1",
        headers={"X-User-Role": "finance_manager"},
        json={"is_enabled": True},
    )

    assert response.status_code == 200
    assert response.json() == {
        "position_group": "Шаурмист",
        "category": "category_1",
        "is_enabled": True,
    }


def test_put_payroll_availability_manager_returns_403(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_set_role_category_availability(*_args, **_kwargs):
        raise AssertionError("manager must not write payroll availability")

    monkeypatch.setattr(
        payroll_config_routes,
        "set_role_category_availability",
        fake_set_role_category_availability,
    )

    response = client.put(
        "/api/v1/payroll/config/availability/Шаурмист/category_1",
        headers={"X-User-Role": "manager"},
        json={"is_enabled": True},
    )

    assert response.status_code == 403


def test_get_revenue_tiers_returns_current_rows(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _tier(
        min_revenue=140000,
        max_revenue=190000,
        rate_percent=0.045,
        effective_from=date(2026, 1, 1),
    )

    async def fake_list_revenue_tiers(_session, *, history: bool = False):
        assert history is False
        return [current]

    monkeypatch.setattr(payroll_config_routes, "list_revenue_tiers", fake_list_revenue_tiers)

    response = client.get(
        "/api/v1/payroll/config/revenue-tiers",
        headers={"X-User-Role": "manager"},
    )

    assert response.status_code == 200
    assert response.json()[0]["rate_percent"] == 0.045


def test_put_revenue_tiers_creates_new_version(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = [
        _tier(
            min_revenue=50000,
            max_revenue=140000,
            rate_percent=0.035,
            effective_from=date(2026, 1, 1),
        )
    ]

    async def fake_replace_revenue_tier_versions(_session, payloads):
        payloads = list(payloads)
        store[0]["effective_to"] = payloads[0].effective_from
        created = [
            {
                "id": uuid.uuid4(),
                **payload.model_dump(),
                "created_at": datetime(2026, 5, 28, 11, 0, tzinfo=UTC),
            }
            for payload in payloads
        ]
        store.extend(created)
        return created

    monkeypatch.setattr(
        payroll_config_routes,
        "replace_revenue_tier_versions",
        fake_replace_revenue_tier_versions,
    )

    response = client.put(
        "/api/v1/payroll/config/revenue-tiers",
        headers={"X-User-Role": "finance_manager"},
        json=[
            {
                "min_revenue": 50000,
                "max_revenue": 140000,
                "rate_percent": 0.04,
                "effective_from": "2026-06-01",
                "effective_to": None,
            }
        ],
    )

    assert response.status_code == 200
    assert response.json()[0]["effective_from"] == "2026-06-01"
    assert response.json()[0]["rate_percent"] == 0.04
    assert store[0]["effective_to"] == date(2026, 6, 1)


def test_get_category_coefficients_returns_normalized_category_2(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [
        _coefficient(category="category_1", coefficient=3, effective_from=date(2026, 1, 1)),
        _coefficient(category="category_2", coefficient=2.25, effective_from=date(2026, 5, 28)),
        _coefficient(category="category_3", coefficient=1.5, effective_from=date(2026, 1, 1)),
        _coefficient(category="intern", coefficient=0, effective_from=date(2026, 1, 1)),
        _coefficient(category="freelancer", coefficient=0, effective_from=date(2026, 1, 1)),
    ]

    async def fake_list_category_coefficients(_session, *, history: bool = False):
        assert history is False
        return current

    monkeypatch.setattr(
        payroll_config_routes,
        "list_category_coefficients",
        fake_list_category_coefficients,
    )

    response = client.get(
        "/api/v1/payroll/config/category-coefficients",
        headers={"X-User-Role": "manager"},
    )

    coefficients = {item["category"]: item["coefficient"] for item in response.json()}

    assert response.status_code == 200
    assert coefficients["category_2"] == 2.25


def test_put_category_coefficients_requires_finance_manager(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_replace_category_coefficient_versions(*_args, **_kwargs):
        raise AssertionError("manager must not write category coefficients")

    monkeypatch.setattr(
        payroll_config_routes,
        "replace_category_coefficient_versions",
        fake_replace_category_coefficient_versions,
    )

    response = client.put(
        "/api/v1/payroll/config/category-coefficients",
        headers={"X-User-Role": "manager"},
        json=[
            {
                "category": "category_1",
                "coefficient": 3,
                "effective_from": "2026-06-01",
                "effective_to": None,
            }
        ],
    )

    assert response.status_code == 403

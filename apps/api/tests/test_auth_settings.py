from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.v1.routes import auth as auth_routes
from app.auth import CurrentUser, current_user
from app.core.security import create_access_token, hash_password, verify_password
from app.main import create_app
from app.services import settings_service


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()
    test_client.close()


def _user(*roles: str) -> CurrentUser:
    return CurrentUser(
        id=uuid.uuid4(),
        email="user@teplo.local",
        full_name="Test User",
        roles=tuple(roles),
    )


def _setting(key: str, value: Any, category: str = "Платёжный календарь") -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "key": key,
        "value": value,
        "value_type": "object" if isinstance(value, dict) else "number",
        "category": category,
        "description": "Test setting",
        "updated_at": datetime(2026, 5, 27, 10, 0, tzinfo=UTC),
        "updated_by_user_id": None,
        "updated_by_user_name": None,
    }


@pytest.fixture()
def fake_settings(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    store = {
        "payment_calendar.auto_match_tolerance": _setting(
            "payment_calendar.auto_match_tolerance",
            {"relative": 0.1, "date_window": "same_month"},
        ),
        "fixed_asset_threshold": _setting("fixed_asset_threshold", 5000, "Учёт ОС"),
    }
    history: list[dict[str, Any]] = []

    async def list_settings(_session, category: str | None = None):
        settings = list(store.values())
        if category is not None:
            settings = [setting for setting in settings if setting["category"] == category]
        return settings

    async def get_setting(_session, key: str):
        try:
            return store[key]
        except KeyError:
            raise settings_service.SettingNotFoundError(key) from None

    async def write_setting(_session, key: str, value: Any, changed_by_user_id: uuid.UUID):
        setting = await get_setting(_session, key)
        old_value = setting["value"]
        setting["value"] = value
        setting["updated_by_user_id"] = changed_by_user_id
        setting["updated_by_user_name"] = "Test User"
        history.append(
            {
                "id": uuid.uuid4(),
                "setting_id": setting["id"],
                "old_value": old_value,
                "new_value": value,
                "changed_at": datetime(2026, 5, 27, 10, 5, tzinfo=UTC),
                "changed_by_user_id": changed_by_user_id,
                "changed_by_user_name": "Test User",
            }
        )
        return setting

    async def get_setting_history(_session, key: str):
        setting = await get_setting(_session, key)
        return [item for item in history if item["setting_id"] == setting["id"]]

    monkeypatch.setattr(settings_service, "list_settings", list_settings)
    monkeypatch.setattr(settings_service, "get_setting", get_setting)
    monkeypatch.setattr(settings_service, "write_setting", write_setting)
    monkeypatch.setattr(settings_service, "get_setting_history", get_setting_history)
    return {"store": store, "history": history}


def test_login_with_valid_password_returns_tokens(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user("finance_manager")
    password_hash = hash_password("secret-password")

    async def authenticate_user(_session, email: str, password: str):
        if email == user.email and verify_password(password, password_hash):
            return user
        return None

    monkeypatch.setattr(auth_routes, "authenticate_user", authenticate_user)

    response = client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "secret-password"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["user"]["roles"] == ["finance_manager"]
    assert "teplo_refresh_token" in response.cookies


def test_login_with_invalid_password_returns_401(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user("finance_manager")
    password_hash = hash_password("secret-password")

    async def authenticate_user(_session, email: str, password: str):
        if email == user.email and verify_password(password, password_hash):
            return user
        return None

    monkeypatch.setattr(auth_routes, "authenticate_user", authenticate_user)

    response = client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "wrong-password"},
    )

    assert response.status_code == 401


def test_protected_endpoint_without_token_returns_401(client: TestClient) -> None:
    response = client.get("/api/v1/settings")

    assert response.status_code == 401


def test_protected_endpoint_with_expired_token_returns_401(client: TestClient) -> None:
    expired_token = create_access_token(
        str(uuid.uuid4()), expires_delta=timedelta(minutes=-1)
    )

    response = client.get(
        "/api/v1/settings",
        headers={"Authorization": f"Bearer {expired_token}"},
    )

    assert response.status_code == 401


def test_get_settings_authorized_returns_ok(
    client: TestClient, fake_settings: dict[str, Any]
) -> None:
    client.app.dependency_overrides[current_user] = lambda: _user("accountant")

    response = client.get("/api/v1/settings", params={"category": "Учёт ОС"})

    assert response.status_code == 200
    assert [item["key"] for item in response.json()] == ["fixed_asset_threshold"]


def test_put_setting_manager_returns_403(
    client: TestClient, fake_settings: dict[str, Any]
) -> None:
    client.app.dependency_overrides[current_user] = lambda: _user("manager")

    response = client.put(
        "/api/v1/settings/payment_calendar.auto_match_tolerance",
        json={"value": {"relative": 0.12, "date_window": "same_month"}},
    )

    assert response.status_code == 403


def test_put_setting_finance_manager_non_critical_updates_history(
    client: TestClient, fake_settings: dict[str, Any]
) -> None:
    user = _user("finance_manager")
    client.app.dependency_overrides[current_user] = lambda: user

    response = client.put(
        "/api/v1/settings/payment_calendar.auto_match_tolerance",
        json={"value": {"relative": 0.12, "date_window": "same_month"}},
    )

    assert response.status_code == 200
    assert response.json()["value"]["relative"] == 0.12
    assert len(fake_settings["history"]) == 1
    assert fake_settings["history"][0]["old_value"]["relative"] == 0.1
    assert fake_settings["history"][0]["changed_by_user_id"] == user.id


def test_put_setting_finance_manager_critical_returns_403(
    client: TestClient, fake_settings: dict[str, Any]
) -> None:
    client.app.dependency_overrides[current_user] = lambda: _user("finance_manager")

    response = client.put(
        "/api/v1/settings/fixed_asset_threshold",
        json={"value": 7000},
    )

    assert response.status_code == 403


def test_put_setting_owner_can_update_critical(
    client: TestClient, fake_settings: dict[str, Any]
) -> None:
    client.app.dependency_overrides[current_user] = lambda: _user("owner")

    response = client.put(
        "/api/v1/settings/fixed_asset_threshold",
        json={"value": 7000},
    )

    assert response.status_code == 200
    assert response.json()["value"] == 7000

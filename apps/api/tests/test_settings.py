from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth import CurrentUser, current_user
from app.main import create_app
from app.services import settings_service


def _user(*roles: str) -> CurrentUser:
    return CurrentUser(
        id=uuid.uuid4(),
        email="owner@teplo.local",
        full_name="Owner",
        roles=tuple(roles),
    )


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()
    test_client.close()


def _setting(key: str, value: Any) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "key": key,
        "value": value,
        "value_type": "number",
        "category": "Учёт ОС",
        "display_name": "Тестовая настройка",
        "description": "Test setting",
        "widget_type": "number",
        "widget_options": None,
        "unit": "₽",
        "is_critical": settings_service.is_critical_setting_key(key),
        "updated_at": datetime(2026, 5, 27, 10, 0, tzinfo=UTC),
        "updated_by_user_id": None,
        "updated_by_user_name": None,
    }


def test_get_setting_by_key_authorized_returns_value(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def get_setting(_session, key: str):
        assert key == "fixed_asset_threshold"
        return _setting(key, 5000)

    monkeypatch.setattr(settings_service, "get_setting", get_setting)
    client.app.dependency_overrides[current_user] = lambda: _user("accountant")

    response = client.get("/api/v1/settings/fixed_asset_threshold")

    assert response.status_code == 200
    assert response.json()["value"] == 5000


def test_get_setting_history_authorized_returns_entries(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    setting_id = uuid.uuid4()

    async def get_setting_history(_session, key: str):
        assert key == "fixed_asset_threshold"
        return [
            {
                "id": uuid.uuid4(),
                "setting_id": setting_id,
                "old_value": 5000,
                "new_value": 7000,
                "changed_at": datetime(2026, 5, 27, 10, 5, tzinfo=UTC),
                "changed_by_user_id": uuid.uuid4(),
                "changed_by_user_name": "Owner",
            }
        ]

    monkeypatch.setattr(settings_service, "get_setting_history", get_setting_history)
    client.app.dependency_overrides[current_user] = lambda: _user("owner")

    response = client.get("/api/v1/settings/fixed_asset_threshold/history")

    assert response.status_code == 200
    assert response.json()[0]["old_value"] == 5000
    assert response.json()[0]["new_value"] == 7000


def test_get_setting_history_missing_key_returns_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def get_setting_history(_session, key: str):
        raise settings_service.SettingNotFoundError(key)

    monkeypatch.setattr(settings_service, "get_setting_history", get_setting_history)
    client.app.dependency_overrides[current_user] = lambda: _user("owner")

    response = client.get("/api/v1/settings/missing/history")

    assert response.status_code == 404


def test_validate_setting_value_accepts_nested_percent() -> None:
    setting = SimpleNamespace(widget_type="percent", widget_options={"value_path": "relative"})

    settings_service.validate_setting_value(
        setting,
        {"relative": 0.1, "date_window": "same_month"},
    )


def test_validate_setting_value_rejects_out_of_range_percent() -> None:
    setting = SimpleNamespace(widget_type="percent", widget_options=None)

    with pytest.raises(settings_service.SettingValidationError):
        settings_service.validate_setting_value(setting, 1.5)


def test_validate_setting_value_rejects_unknown_select_value() -> None:
    setting = SimpleNamespace(
        widget_type="select",
        widget_options={"options": [{"value": "calendar", "label": "Календарная"}]},
    )

    with pytest.raises(settings_service.SettingValidationError):
        settings_service.validate_setting_value(setting, "workday")

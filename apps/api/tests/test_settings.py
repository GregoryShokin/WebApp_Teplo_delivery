from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.deps import CurrentActor, get_current_actor
from app.auth import CurrentUser
from app.auth.permissions import ALL_PERMISSION_CODES
from app.main import create_app
from app.services import iiko_sync, payroll_config, settings_service


def _user(*roles: str) -> CurrentUser:
    return CurrentUser(
        id=uuid.uuid4(),
        email="owner@teplo.local",
        full_name="Owner",
        roles=tuple(roles),
    )


def _actor(
    *roles: str,
    permissions: set[str] | frozenset[str] = frozenset(),
) -> CurrentActor:
    return CurrentActor(
        roles=frozenset(roles),
        user_id=uuid.uuid4(),
        permissions=frozenset(permissions),
        permissions_loaded=True,
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
    client.app.dependency_overrides[get_current_actor] = lambda: _actor(
        "manager",
        permissions={"settings.general.read"},
    )

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
    client.app.dependency_overrides[get_current_actor] = lambda: _actor(
        "owner",
        permissions=ALL_PERMISSION_CODES,
    )

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
    client.app.dependency_overrides[get_current_actor] = lambda: _actor(
        "owner",
        permissions=ALL_PERMISSION_CODES,
    )

    response = client.get("/api/v1/settings/missing/history")

    assert response.status_code == 404


def test_put_substitute_pairs_accepts_request_models(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[payroll_config.SubstitutePair] = []

    async def set_substitute_pairs(_session, pairs, _user):
        normalized = payroll_config._validate_substitute_pairs(pairs)
        captured.extend(normalized)
        return normalized

    async def refresh_role_review_for_all_employees(_session, *, force: bool = False) -> None:
        assert force is True

    monkeypatch.setattr(payroll_config, "set_substitute_pairs", set_substitute_pairs)
    monkeypatch.setattr(
        iiko_sync,
        "refresh_role_review_for_all_employees",
        refresh_role_review_for_all_employees,
    )
    client.app.dependency_overrides[get_current_actor] = lambda: _actor(
        "admin",
        permissions=ALL_PERMISSION_CODES,
    )

    response = client.put(
        "/api/v1/settings/substitute-pairs",
        json={
            "pairs": [
                {
                    "from_position": "Управляющий",
                    "to_position": "Повар",
                    "add_to_schedule": True,
                }
            ]
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "pairs": [
            {
                "from_position": "Управляющий",
                "to_position": "Повар",
                "add_to_schedule": True,
            }
        ]
    }
    assert [pair.model_dump() for pair in captured] == [
        {"from_position": "Управляющий", "to_position": "Повар", "add_to_schedule": True}
    ]


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


def test_validate_setting_value_accepts_weekday_premium() -> None:
    setting = SimpleNamespace(widget_type="weekday_premium", widget_options=None)

    settings_service.validate_setting_value(
        setting,
        {
            "monday": 0,
            "tuesday": 0,
            "wednesday": 0,
            "thursday": 0,
            "friday": 200,
            "saturday": 200,
            "sunday": 0,
        },
    )


def test_validate_setting_value_accepts_weekday_premium_amount_and_threshold() -> None:
    setting = SimpleNamespace(widget_type="weekday_premium", widget_options=None)

    settings_service.validate_setting_value(
        setting,
        {"amount": 250, "threshold_hours": 8},
    )


def test_validate_setting_value_rejects_negative_weekday_premium() -> None:
    setting = SimpleNamespace(widget_type="weekday_premium", widget_options=None)

    with pytest.raises(settings_service.SettingValidationError):
        settings_service.validate_setting_value(setting, {"friday": -1})

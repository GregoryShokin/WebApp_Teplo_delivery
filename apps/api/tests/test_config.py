from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_production_settings_reject_placeholder_jwt_secret() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            environment="production",
            jwt_secret_key="change-me-in-local-env-change-me",
            auth_cookie_secure=True,
            teplo_bank_client_mode="live",
        )

    assert "JWT_SECRET_KEY" in str(exc_info.value)


def test_production_settings_require_secure_cookie() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            environment="production",
            jwt_secret_key="a" * 64,
            auth_cookie_secure=False,
            teplo_bank_client_mode="live",
        )

    assert "AUTH_COOKIE_SECURE" in str(exc_info.value)


def test_production_settings_require_live_bank_mode() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            environment="production",
            jwt_secret_key="a" * 64,
            auth_cookie_secure=True,
            teplo_bank_client_mode="mock",
        )

    assert "TEPLO_BANK_CLIENT_MODE" in str(exc_info.value)


def test_production_settings_accept_realistic_values() -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        jwt_secret_key="a" * 64,
        auth_cookie_secure=True,
        teplo_bank_client_mode="live",
    )

    assert settings.environment == "production"

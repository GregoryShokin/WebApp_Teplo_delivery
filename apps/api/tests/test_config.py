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


def test_production_settings_require_webhook_token() -> None:
    """Без токена вебхука прод не собирается — и это единственная защита эндпоинта.

    Вебхук «Статус платежа» мутирует финучёт (гасит и откатывает накладные), а фильтр по IP
    за прокси ненадёжен. Настройки читаются на импорте (``app/db/session.py``), поэтому
    отсутствие токена валит подъём api и scheduler целиком, а не отдельный запрос — что и
    делает предполётную проверку в ``deploy/check-prod-secrets.sh`` обязательной.
    """
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            environment="production",
            jwt_secret_key="a" * 64,
            auth_cookie_secure=True,
            teplo_bank_client_mode="live",
        )

    assert "TBANK_WEBHOOK_TOKEN" in str(exc_info.value)


def test_production_settings_accept_realistic_values() -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        jwt_secret_key="a" * 64,
        auth_cookie_secure=True,
        teplo_bank_client_mode="live",
        tbank_webhook_token="a" * 32,
    )

    assert settings.environment == "production"


def test_production_settings_reject_empty_tbank_payment_url() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            environment="production",
            jwt_secret_key="a" * 64,
            auth_cookie_secure=True,
            teplo_bank_client_mode="live",
            tbank_webhook_token="a" * 32,
            tbank_payment_base_url="",
        )

    assert "TBANK_PAYMENT_BASE_URL" in str(exc_info.value)

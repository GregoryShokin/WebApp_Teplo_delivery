from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PRODUCTION_ENVIRONMENTS = {"prod", "production"}
PLACEHOLDER_SECRET_MARKERS = (
    "change-me",
    "placeholder",
    "replace-with",
    "example.com",
)


def _looks_like_placeholder(value: str | None) -> bool:
    if not value:
        return True
    normalized = value.strip().casefold()
    return any(marker in normalized for marker in PLACEHOLDER_SECRET_MARKERS)


class Settings(BaseSettings):
    app_name: str = "Teplo API"
    app_version: str = "0.1.0"
    environment: str = "local"

    database_url: str = "postgresql+asyncpg://teplo:teplo@localhost:5432/teplo"

    jwt_secret_key: str = Field(default="change-me-in-local-env-change-me", min_length=32)
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 7
    auth_refresh_cookie_name: str = "teplo_refresh_token"
    auth_cookie_secure: bool = False

    backend_cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ]

    scheduler_enabled: bool = True
    employee_sync_enabled: bool = True
    employee_sync_interval_hours: int = 6
    teplo_bank_client_mode: Literal["mock", "live"] = "mock"
    bank_sync_providers: str = "tbank"
    bank_client_timeout_seconds: float = 90

    sber_api_base_url: str = "https://fintech.sberbank.ru:9443/fintech/api"
    sber_api_ca_bundle_path: str | None = None
    sber_api_account_number: str | None = None

    tbank_api_base_url: str = "https://business.tbank.ru/openapi"
    tbank_api_account_number: str | None = None
    tbank_payment_base_url: str = "https://secured-openapi.tbank.ru"
    tbank_api_timeout_seconds: float = 90

    @model_validator(mode="after")
    def validate_production_settings(self) -> Settings:
        if self.environment.casefold() not in PRODUCTION_ENVIRONMENTS:
            return self

        errors: list[str] = []
        if _looks_like_placeholder(self.jwt_secret_key):
            errors.append("JWT_SECRET_KEY must be a real production secret")
        if not self.auth_cookie_secure:
            errors.append("AUTH_COOKIE_SECURE must be true in production")
        if self.teplo_bank_client_mode != "live":
            errors.append("TEPLO_BANK_CLIENT_MODE must be live in production")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    @property
    def TEPLO_BANK_CLIENT_MODE(self) -> Literal["mock", "live"]:
        return self.teplo_bank_client_mode

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    bank_client_timeout_seconds: float = 90

    sber_api_base_url: str = "https://fintech.sberbank.ru:9443/fintech/api"
    sber_api_ca_bundle_path: str | None = None
    sber_api_account_number: str | None = None

    tbank_api_base_url: str = "https://business.tbank.ru/openapi"
    tbank_api_account_number: str | None = None

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

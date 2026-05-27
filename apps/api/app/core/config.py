from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Teplo API"
    app_version: str = "0.1.0"
    environment: str = "local"

    database_url: str = "postgresql+asyncpg://teplo:teplo@localhost:5432/teplo"

    jwt_secret_key: str = Field(default="change-me-in-local-env", min_length=16)
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60

    backend_cors_origins: list[str] = ["http://localhost:5173"]

    scheduler_enabled: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()

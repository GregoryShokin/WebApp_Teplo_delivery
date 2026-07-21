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
    counterparty_invoice_sync_enabled: bool = True
    # Periodic iiko invoice sync runs often on a NARROW window (fresh supplies appear via
    # DocsInbox within minutes); the full window is reserved for the manual «Синхронизировать»
    # button, which back-fills older / amended invoices on demand.
    counterparty_invoice_sync_interval_minutes: int = 15
    counterparty_invoice_sync_cron_days: int = 7
    counterparty_invoice_sync_days: int = 30
    counterparty_match_window_days: int = 7
    teplo_bank_client_mode: Literal["mock", "live"] = "mock"
    bank_sync_providers: str = "tbank"
    bank_client_timeout_seconds: float = 90

    sber_api_base_url: str = "https://fintech.sberbank.ru:9443/fintech/api"
    sber_api_token_url: str = "https://sbi.sberbank.ru:9443/ic/sso/api/v2/oauth/token"
    sber_api_client_id: str | None = None
    sber_api_ca_bundle_path: str | None = None
    sber_api_account_number: str | None = None

    tbank_api_base_url: str = "https://business.tbank.ru/openapi"
    tbank_api_account_number: str | None = None
    # Payment draft creation uses the same bearer-token Business API as statements
    # (POST /api/v1/payment/create), not the mTLS host-to-host endpoint.
    tbank_payment_base_url: str = "https://business.tbank.ru/openapi"
    tbank_api_timeout_seconds: float = 90
    # Webhook «Статус платежа» T-Банка (входящие POST банк→мы). Токен сверяется с заголовком
    # Authorization: Bearer; список IP (CSV) — опциональный whitelist 6 IP банка. Пусто =
    # проверка отключена (dev). Заявка на подключение — письмом на openapi@tbank.ru.
    tbank_webhook_token: str | None = None
    tbank_webhook_allowed_ips: str = ""

    # Webhook iikoCloud (входящие POST iiko→мы): открытие/закрытие смен курьеров realtime.
    # Токен сверяется с Authorization: Bearer; список IP (CSV) — опциональный whitelist.
    # Пусто = проверка отключена (dev). Тот же токен прописывается в подписке iikoCloud.
    iiko_webhook_token: str | None = None
    iiko_webhook_allowed_ips: str = ""

    # --- СБИС (Saby) ЭДО: зеркало входящих документов для сверки с iiko-накладными ---
    # Сервисная авторизация внешнего приложения (OAuth 2): хватает пары app_client_id +
    # secret_key из кабинета Saby (прокидываются из ../.env как SBIS_*). Токен передаётся
    # заголовком X-SBISAccessToken; на 401 переполучаем. Пусто = синк пропускается
    # (джоба не падает, просто логирует «не настроено»).
    sbis_app_client_id: str | None = None
    sbis_secret_key: str | None = None
    sbis_auth_url: str = "https://online.sbis.ru/oauth/service/"
    sbis_service_url: str = "https://online.sbis.ru/service/?srv=1"
    sbis_sync_enabled: bool = True
    sbis_sync_interval_minutes: int = 60
    # Насколько вглубь запрашивать реестр «Входящие» при каждом проходе. Идемпотентность —
    # по идентификатору документа СБИС, повторный проход того же окна бесплатен.
    sbis_sync_lookback_days: int = 30
    # Счета в СБИС — отдельный реестр «СчетВх» с отдельным правом сервисного доступа:
    # без права выборка отдаёт пусто (не ошибку), поэтому включено по умолчанию.
    sbis_fetch_invoice_registry: bool = True
    sbis_http_timeout_seconds: float = 60

    # --- Почта: ingest счетов/УПД для «Страницы на оплату» (Фаза 1) ---
    # Креды обоих ящиков прокидываются из ../.env (MAILRU_*). Пусто = ingest пропускается
    # (джоба не падает, просто логирует «не настроено»).
    mailru_email: str | None = None
    mailru_app_password: str | None = None
    mailru_workmail: str | None = None
    mailru_workmail_password: str | None = None
    mailru_imap_host: str = "imap.mail.ru"
    mailru_imap_port: int = 993
    # Циклический опрос почты планировщиком (realtime-канала у mail.ru нет — только поллинг).
    mail_poll_enabled: bool = True
    mail_poll_interval_minutes: int = 10
    # Насколько вглубь сканировать ящик при каждом проходе (IMAP SEARCH SINCE). Идемпотентность
    # по SHA-256 вложения делает повторный проход того же письма бесплатным.
    mail_poll_lookback_days: int = 14

    # Распознавание счёта — гибрид: детерминированный парсер (регексы по тексту PDF) +
    # LLM-фолбэк (Claude по самому PDF) для незнакомых макетов. Без ключа работает только
    # детерминированный слой, неуверенные счета уходят в «требует проверки».
    anthropic_api_key: str | None = None
    invoice_recognition_model: str = "claude-sonnet-4-6"
    # Ниже порога уверенности счёт не материализуется в накладную, а ждёт оператора.
    invoice_recognition_min_confidence: float = 0.75

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
        # Webhook «Статус платежа» мутирует финучёт (гасит/откатывает накладные). Токен —
        # основная защита (IP по XFF за прокси ненадёжен), поэтому в проде он обязателен.
        if _looks_like_placeholder(self.tbank_webhook_token):
            errors.append("TBANK_WEBHOOK_TOKEN must be set in production")
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

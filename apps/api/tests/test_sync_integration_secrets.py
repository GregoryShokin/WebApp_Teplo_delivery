from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import SourceCredential
from app.scripts import sync_integration_secrets

SECRET_ENV_KEYS = (
    *sync_integration_secrets.IIKO_ENV_KEYS,
    *sync_integration_secrets.IIKO_CLOUD_ENV_KEYS,
    sync_integration_secrets.TBANK_BEARER_TOKEN.env_name,
    sync_integration_secrets.TBANK_ACCOUNT_NUMBER_ENV,
    *(spec.env_name for spec in sync_integration_secrets.SBER_CREDENTIALS),
    sync_integration_secrets.SBER_ACCOUNT_NUMBER_ENV,
)


@pytest.fixture(autouse=True)
def _clear_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in SECRET_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _use_test_session_factory(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(sync_integration_secrets, "AsyncSessionLocal", session_factory)


def _set_iiko_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IIKO_SERVER_BASE_URL", "https://iiko.example.test")
    monkeypatch.setenv("IIKO_SERVER_LOGIN", "iiko-login")
    monkeypatch.setenv("IIKO_SERVER_PASSWORD", "iiko-password")
    _set_iiko_cloud_env(monkeypatch)


def _set_iiko_cloud_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IIKO_CLOUD_APP_ID", "cloud-app-id")
    monkeypatch.setenv("IIKO_CLOUD_API_LOGIN", "cloud-api-login")
    monkeypatch.setenv("IIKO_CLOUD_CLIENT_SECRET", "cloud-client-secret")


async def _load_credentials(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[SourceCredential]:
    async with session_factory() as session:
        return (
            await session.scalars(
                select(SourceCredential).order_by(
                    SourceCredential.provider,
                    SourceCredential.credential_kind,
                    SourceCredential.created_at,
                )
            )
        ).all()


def _assert_no_secret_values(output: str) -> None:
    leaked_values = (
        "https://iiko.example.test",
        "iiko-login",
        "iiko-password",
        "cloud-app-id",
        "cloud-api-login",
        "cloud-client-secret",
        "first-token",
        "second-token",
        "unused-sber-token",
        "sber-access-token",
        "sber-refresh-token",
        "sber-client-secret",
        "40702810900000000001",
    )
    for value in leaked_values:
        assert value not in output


@pytest.mark.asyncio
async def test_check_missing_all_required_env_fails_without_secret_values(
    monkeypatch: pytest.MonkeyPatch,
    async_session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _use_test_session_factory(monkeypatch, async_session_factory)

    code = await sync_integration_secrets.check()

    output = capsys.readouterr().out
    assert code == 1
    assert output.splitlines() == [
        "iiko_env=missing",
        "iiko_cloud_env=missing",
        "tbank_bearer_token=env:missing db:missing",
    ]
    _assert_no_secret_values(output)


@pytest.mark.asyncio
async def test_check_iiko_set_and_tbank_env_missing_fails(
    monkeypatch: pytest.MonkeyPatch,
    async_session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _use_test_session_factory(monkeypatch, async_session_factory)
    _set_iiko_env(monkeypatch)
    monkeypatch.setenv("SBER_API_ACCESS_TOKEN", "unused-sber-token")

    code = await sync_integration_secrets.check()

    output = capsys.readouterr().out
    assert code == 1
    assert output.splitlines() == [
        "iiko_env=set",
        "iiko_cloud_env=set",
        "tbank_bearer_token=env:missing db:missing",
    ]
    assert "sber" not in output.casefold()
    _assert_no_secret_values(output)


@pytest.mark.asyncio
async def test_check_iiko_cloud_env_missing_fails(
    monkeypatch: pytest.MonkeyPatch,
    async_session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cloud-кред нет, всё остальное на месте → деплой обязан упасть здесь, а не на
    первой накладной с «auth: 'IIKO_CLOUD_APP_ID'»."""

    _use_test_session_factory(monkeypatch, async_session_factory)
    _set_iiko_env(monkeypatch)
    for key in sync_integration_secrets.IIKO_CLOUD_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TBANK_API_ACCESS_TOKEN", "first-token")

    code = await sync_integration_secrets.check()

    output = capsys.readouterr().out
    assert code == 1
    assert output.splitlines() == [
        "iiko_env=set",
        "iiko_cloud_env=missing",
        "tbank_bearer_token=env:set db:missing",
    ]
    _assert_no_secret_values(output)


@pytest.mark.asyncio
async def test_check_tbank_env_set_and_db_missing_fails(
    monkeypatch: pytest.MonkeyPatch,
    async_session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _use_test_session_factory(monkeypatch, async_session_factory)
    _set_iiko_env(monkeypatch)
    monkeypatch.setenv("TBANK_API_ACCESS_TOKEN", "first-token")

    code = await sync_integration_secrets.check()

    output = capsys.readouterr().out
    assert code == 1
    assert output.splitlines() == [
        "iiko_env=set",
        "iiko_cloud_env=set",
        "tbank_bearer_token=env:set db:missing",
    ]
    _assert_no_secret_values(output)


@pytest.mark.asyncio
async def test_sync_tbank_env_creates_bearer_token_and_does_not_store_iiko_env(
    monkeypatch: pytest.MonkeyPatch,
    async_session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _use_test_session_factory(monkeypatch, async_session_factory)
    _set_iiko_env(monkeypatch)
    monkeypatch.setenv("TBANK_API_ACCESS_TOKEN", "first-token")
    monkeypatch.setenv("TBANK_API_ACCOUNT_NUMBER", "40702810900000000001")

    code = await sync_integration_secrets.sync()

    output = capsys.readouterr().out
    rows = await _load_credentials(async_session_factory)
    assert code == 0
    assert output.splitlines() == [
        "iiko_env=set",
        "iiko_cloud_env=set",
        "tbank_bearer_token=created",
    ]
    assert [(row.provider, row.credential_kind, row.is_active) for row in rows] == [
        ("tbank", "bearer_token", True)
    ]
    assert rows[0].value_encrypted == "first-token"
    assert rows[0].metadata_json == {"account_number": "40702810900000000001"}
    _assert_no_secret_values(output)


@pytest.mark.asyncio
async def test_sync_same_tbank_token_is_unchanged_without_duplicate_active(
    monkeypatch: pytest.MonkeyPatch,
    async_session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _use_test_session_factory(monkeypatch, async_session_factory)
    _set_iiko_env(monkeypatch)
    monkeypatch.setenv("TBANK_API_ACCESS_TOKEN", "first-token")

    assert await sync_integration_secrets.sync() == 0
    capsys.readouterr()

    assert await sync_integration_secrets.sync() == 0

    output = capsys.readouterr().out
    rows = await _load_credentials(async_session_factory)
    active_rows = [row for row in rows if row.is_active]
    assert output.splitlines() == [
        "iiko_env=set",
        "iiko_cloud_env=set",
        "tbank_bearer_token=unchanged",
    ]
    assert len(rows) == 1
    assert len(active_rows) == 1
    assert active_rows[0].value_encrypted == "first-token"
    _assert_no_secret_values(output)


@pytest.mark.asyncio
async def test_sync_new_tbank_token_deactivates_old_active_token(
    monkeypatch: pytest.MonkeyPatch,
    async_session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _use_test_session_factory(monkeypatch, async_session_factory)
    _set_iiko_env(monkeypatch)
    monkeypatch.setenv("TBANK_API_ACCESS_TOKEN", "first-token")

    assert await sync_integration_secrets.sync() == 0
    capsys.readouterr()

    monkeypatch.setenv("TBANK_API_ACCESS_TOKEN", "second-token")
    assert await sync_integration_secrets.sync() == 0

    output = capsys.readouterr().out
    rows = await _load_credentials(async_session_factory)
    active_values = [row.value_encrypted for row in rows if row.is_active]
    inactive_values = [row.value_encrypted for row in rows if not row.is_active]
    assert output.splitlines() == [
        "iiko_env=set",
        "iiko_cloud_env=set",
        "tbank_bearer_token=updated",
    ]
    assert active_values == ["second-token"]
    assert inactive_values == ["first-token"]
    _assert_no_secret_values(output)


@pytest.mark.asyncio
async def test_sber_missing_does_not_break_minimal_check(
    monkeypatch: pytest.MonkeyPatch,
    async_session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _use_test_session_factory(monkeypatch, async_session_factory)
    _set_iiko_env(monkeypatch)
    monkeypatch.setenv("TBANK_API_ACCESS_TOKEN", "first-token")

    assert await sync_integration_secrets.sync() == 0
    capsys.readouterr()

    code = await sync_integration_secrets.check()

    output = capsys.readouterr().out
    assert code == 0
    assert output.splitlines() == [
        "iiko_env=set",
        "iiko_cloud_env=set",
        "tbank_bearer_token=env:set db:set",
    ]
    assert "sber" not in output.casefold()
    _assert_no_secret_values(output)


@pytest.mark.asyncio
async def test_sync_sber_oauth_pair_and_mtls_credentials(
    monkeypatch: pytest.MonkeyPatch,
    async_session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _use_test_session_factory(monkeypatch, async_session_factory)
    _set_iiko_env(monkeypatch)
    monkeypatch.setenv("TBANK_API_ACCESS_TOKEN", "first-token")
    monkeypatch.setenv("SBER_API_ACCESS_TOKEN", "sber-access-token")
    monkeypatch.setenv("SBER_API_REFRESH_TOKEN", "sber-refresh-token")
    monkeypatch.setenv("SBER_API_CLIENT_SECRET", "sber-client-secret")
    monkeypatch.setenv("SBER_API_TLS_CERT_PATH", "/run/secrets/sber/client.crt")
    monkeypatch.setenv("SBER_API_TLS_KEY_PATH", "/run/secrets/sber/client.key")
    monkeypatch.setenv("SBER_API_ACCOUNT_NUMBER", "40702810900000000001")

    assert await sync_integration_secrets.sync() == 0

    output = capsys.readouterr().out
    rows = await _load_credentials(async_session_factory)
    active_sber = {
        row.credential_kind: row for row in rows if row.provider == "sber" and row.is_active
    }
    assert set(active_sber) == {
        "access_token",
        "refresh_token",
        "client_secret",
        "mtls_cert_path",
        "mtls_key_path",
    }
    assert active_sber["access_token"].metadata_json == {"account_number": "40702810900000000001"}
    assert output.splitlines() == [
        "iiko_env=set",
        "iiko_cloud_env=set",
        "tbank_bearer_token=created",
        "sber_access_token=created",
        "sber_refresh_token=created",
        "sber_client_secret=created",
        "sber_mtls_cert_path=created",
        "sber_mtls_key_path=created",
    ]
    _assert_no_secret_values(output)

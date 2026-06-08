from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import SourceCredential
from app.scripts import sync_integration_secrets

SECRET_ENV_KEYS = (
    *sync_integration_secrets.IIKO_ENV_KEYS,
    sync_integration_secrets.TBANK_BEARER_TOKEN.env_name,
    sync_integration_secrets.TBANK_ACCOUNT_NUMBER_ENV,
    "SBER_API_ACCESS_TOKEN",
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
        "first-token",
        "second-token",
        "unused-sber-token",
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
        "tbank_bearer_token=env:missing db:missing",
    ]
    assert "sber" not in output.casefold()
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
        "tbank_bearer_token=env:set db:set",
    ]
    assert "sber" not in output.casefold()
    _assert_no_secret_values(output)

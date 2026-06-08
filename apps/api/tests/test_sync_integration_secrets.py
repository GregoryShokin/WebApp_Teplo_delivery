from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.scripts import sync_integration_secrets


class FakeSessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> None:
        return None


def _set_iiko_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IIKO_SERVER_BASE_URL", "https://iiko.example.test")
    monkeypatch.setenv("IIKO_SERVER_LOGIN", "iiko-login")
    monkeypatch.setenv("IIKO_SERVER_PASSWORD", "iiko-password")


@pytest.mark.asyncio
async def test_check_reports_statuses_without_secret_values(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_active_credentials(_session: object) -> dict[tuple[str, str], str]:
        return {}

    monkeypatch.setattr(sync_integration_secrets, "AsyncSessionLocal", FakeSessionContext)
    monkeypatch.setattr(
        sync_integration_secrets,
        "_active_credential_values",
        fake_active_credentials,
    )
    _set_iiko_env(monkeypatch)
    monkeypatch.setenv("TBANK_API_ACCESS_TOKEN", "super-secret-token")
    monkeypatch.setenv("SBER_API_ACCESS_TOKEN", "unused-sber-token")

    code = await sync_integration_secrets.check()

    output = capsys.readouterr().out
    assert code == 1
    assert output.splitlines() == [
        "iiko_env=set",
        "tbank_bearer_token=env:set db:missing",
    ]
    assert "super-secret-token" not in output
    assert "unused-sber-token" not in output
    assert "sber" not in output.casefold()


@pytest.mark.asyncio
async def test_sync_maps_tbank_token_and_does_not_store_iiko_env(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    active_credentials = {("tbank", "bearer_token"): "first-token"}
    calls: list[tuple[str, str, str]] = []

    async def fake_active_credentials(_session: object) -> dict[tuple[str, str], str]:
        return dict(active_credentials)

    async def fake_set_credential(
        _session: object,
        *,
        provider: str,
        kind: str,
        value: str,
    ) -> object:
        calls.append((provider, kind, value))
        active_credentials[(provider, kind)] = value
        return SimpleNamespace(provider=provider, credential_kind=kind)

    monkeypatch.setattr(sync_integration_secrets, "AsyncSessionLocal", FakeSessionContext)
    monkeypatch.setattr(
        sync_integration_secrets,
        "_active_credential_values",
        fake_active_credentials,
    )
    monkeypatch.setattr(sync_integration_secrets, "set_credential", fake_set_credential)
    _set_iiko_env(monkeypatch)

    monkeypatch.setenv("TBANK_API_ACCESS_TOKEN", "second-token")
    assert await sync_integration_secrets.sync() == 0

    output = capsys.readouterr().out
    assert "first-token" not in output
    assert "second-token" not in output
    assert calls == [("tbank", "bearer_token", "second-token")]

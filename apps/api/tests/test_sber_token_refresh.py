from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models import SourceCredential
from app.services.banking.credentials import set_credential
from app.services.banking.sber import SberClient


class RefreshingSberClient(SberClient):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(
            session,
            settings=get_settings().model_copy(
                update={
                    "teplo_bank_client_mode": "live",
                    "sber_api_client_id": "sber-client-id",
                }
            ),
        )
        self.refresh_request: dict[str, str] | None = None

    async def _request_token_refresh(
        self, *, refresh_token: str, client_secret: str
    ) -> dict[str, object]:
        self.refresh_request = {
            "refresh_token": refresh_token,
            "client_secret": client_secret,
        }
        return {
            "access_token": "fresh-access-token",
            "refresh_token": "fresh-refresh-token",
            "expires_in": 3600,
        }


async def _seed_sber_oauth_credentials(
    session: AsyncSession,
    *,
    access_expires_at: datetime | None,
) -> None:
    await set_credential(
        session,
        provider="sber",
        kind="access_token",
        value="old-access-token",
        expires_at=access_expires_at,
        metadata_json={"account_number": "40702810900000000001"},
    )
    await set_credential(
        session,
        provider="sber",
        kind="refresh_token",
        value="old-refresh-token",
    )
    await set_credential(
        session,
        provider="sber",
        kind="client_secret",
        value="client-secret",
    )


@pytest.mark.asyncio
async def test_expired_sber_access_token_is_refreshed_and_pair_rotated(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _seed_sber_oauth_credentials(
            session, access_expires_at=datetime.now(UTC) - timedelta(seconds=1)
        )
        client = RefreshingSberClient(session)

        token = await client._access_token()
        credentials = await session.scalars(
            select(SourceCredential).where(
                SourceCredential.provider == "sber",
                SourceCredential.is_active.is_(True),
            )
        )
        active = {row.credential_kind: row for row in credentials.all()}

    assert token == "fresh-access-token"
    assert client.refresh_request == {
        "refresh_token": "old-refresh-token",
        "client_secret": "client-secret",
    }
    assert active["access_token"].value_encrypted == "fresh-access-token"
    assert active["refresh_token"].value_encrypted == "fresh-refresh-token"
    assert active["access_token"].expires_at is not None
    assert active["access_token"].metadata_json == {"account_number": "40702810900000000001"}


@pytest.mark.asyncio
async def test_sber_request_retries_once_after_401_with_refreshed_token(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _seed_sber_oauth_credentials(session, access_expires_at=None)
        client = RefreshingSberClient(session)
        seen_authorizations: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen_authorizations.append(request.headers["Authorization"])
            if len(seen_authorizations) == 1:
                return httpx.Response(401)
            return httpx.Response(200, json={"transactions": []})

        async with httpx.AsyncClient(
            base_url="https://sber.example",
            headers={"Authorization": "Bearer old-access-token"},
            transport=httpx.MockTransport(handler),
        ) as http_client:
            payload = await client._get_json(http_client, "/v2/statement/transactions", {})

    assert payload == {"transactions": []}
    assert seen_authorizations == ["Bearer old-access-token", "Bearer fresh-access-token"]

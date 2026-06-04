from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import SourceCredential


def test_credentials_crud_rotates_active_value_and_soft_deletes(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    headers = {"X-User-Role": "finance_manager"}
    expires_at = datetime(2026, 6, 3, 12, 0, tzinfo=UTC).isoformat()

    first = client.post(
        "/api/v1/dds/credentials",
        headers=headers,
        json={
            "provider": "sber",
            "credential_kind": "access_token",
            "value": "first-token",
            "expires_at": expires_at,
            "metadata": {"source": "test"},
        },
    )
    second = client.post(
        "/api/v1/dds/credentials",
        headers=headers,
        json={
            "provider": "sber",
            "credential_kind": "access_token",
            "value": "second-token",
            "expires_at": expires_at,
        },
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert "value_encrypted" not in first.json()
    assert "value_encrypted" not in second.json()

    active = client.get("/api/v1/dds/credentials", headers=headers)
    assert active.status_code == 200
    active_body = active.json()
    assert len(active_body) == 1
    assert active_body[0]["id"] == second.json()["id"]
    assert active_body[0]["provider"] == "sber"
    assert active_body[0]["credential_kind"] == "access_token"
    assert "value_encrypted" not in active_body[0]

    rows = asyncio.run(_load_credentials(async_session_factory))
    assert len(rows) == 2
    assert [row.value_encrypted for row in rows if row.is_active] == ["second-token"]
    assert [row.value_encrypted for row in rows if not row.is_active] == ["first-token"]

    deleted = client.delete(f"/api/v1/dds/credentials/{second.json()['id']}", headers=headers)
    assert deleted.status_code == 204

    active_after_delete = client.get("/api/v1/dds/credentials", headers=headers)
    assert active_after_delete.status_code == 200
    assert active_after_delete.json() == []


async def _load_credentials(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[SourceCredential]:
    async with session_factory() as session:
        return (
            await session.scalars(
                select(SourceCredential)
                .where(
                    SourceCredential.provider == "sber",
                    SourceCredential.credential_kind == "access_token",
                )
                .order_by(SourceCredential.created_at)
            )
        ).all()

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import SourceCredential
from app.scripts.set_credential import set_credential


@pytest.mark.asyncio
async def test_set_credential_rotates_active_value_and_expires_at(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    expires_at = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)
    async with async_session_factory() as session:
        await set_credential(
            session,
            provider="tbank",
            kind="bearer_token",
            value="first-token",
            expires_at=expires_at,
        )
        await set_credential(
            session,
            provider="tbank",
            kind="bearer_token",
            value="second-token",
            expires_at=expires_at,
        )

        rows = (
            await session.scalars(
                select(SourceCredential)
                .where(
                    SourceCredential.provider == "tbank",
                    SourceCredential.credential_kind == "bearer_token",
                )
                .order_by(SourceCredential.created_at)
            )
        ).all()

    assert len(rows) == 2
    active = [row for row in rows if row.is_active]
    assert len(active) == 1
    assert active[0].value_encrypted == "second-token"
    assert active[0].expires_at == expires_at
    assert [row.value_encrypted for row in rows if not row.is_active] == ["first-token"]

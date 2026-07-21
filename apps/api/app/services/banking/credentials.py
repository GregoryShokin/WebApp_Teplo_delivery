from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SourceCredential

Provider = Literal["sber", "tbank"]
CredentialKind = Literal[
    "access_token",
    "refresh_token",
    "client_secret",
    "bearer_token",
    "mtls_cert_path",
    "mtls_key_path",
]

PROVIDERS = ("sber", "tbank")
KINDS = (
    "access_token",
    "refresh_token",
    "client_secret",
    "bearer_token",
    "mtls_cert_path",
    "mtls_key_path",
)


async def set_credential(
    session: AsyncSession,
    *,
    provider: Provider,
    kind: CredentialKind,
    value: str,
    expires_at: datetime | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> SourceCredential:
    async with session.begin():
        existing = await session.scalars(
            select(SourceCredential).where(
                SourceCredential.provider == provider,
                SourceCredential.credential_kind == kind,
                SourceCredential.is_active.is_(True),
            )
        )
        for credential in existing.all():
            credential.is_active = False
        credential = SourceCredential(
            provider=provider,
            credential_kind=kind,
            value_encrypted=value,
            expires_at=expires_at,
            is_active=True,
            metadata_json=metadata_json,
            status="active",
        )
        session.add(credential)
    return credential

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models import SourceCredential

Provider = Literal["sber", "tbank"]
CredentialKind = Literal[
    "access_token",
    "client_secret",
    "bearer_token",
    "mtls_cert_path",
    "mtls_key_path",
]

PROVIDERS = ("sber", "tbank")
KINDS = ("access_token", "client_secret", "bearer_token", "mtls_cert_path", "mtls_key_path")


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Set an active bank source credential.")
    parser.add_argument("provider", choices=PROVIDERS)
    parser.add_argument("kind", choices=KINDS)
    parser.add_argument("value")
    parser.add_argument("--expires-at", dest="expires_at")
    parser.add_argument("--metadata-json", dest="metadata_json")
    return parser.parse_args()


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_metadata(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise SystemExit("--metadata-json must decode to a JSON object")
    return payload


async def async_main() -> None:
    args = parse_args()
    async with AsyncSessionLocal() as session:
        credential = await set_credential(
            session,
            provider=args.provider,
            kind=args.kind,
            value=args.value,
            expires_at=parse_datetime(args.expires_at),
            metadata_json=parse_metadata(args.metadata_json),
        )
    print(
        f"credential_set provider={credential.provider} "
        f"kind={credential.credential_kind} id={credential.id}"
    )


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()

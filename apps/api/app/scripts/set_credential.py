from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from typing import Any

from app.db.session import AsyncSessionLocal
from app.services.banking.credentials import (
    KINDS,
    PROVIDERS,
    CredentialKind,
    Provider,
    set_credential,
)

__all__ = ["CredentialKind", "Provider", "KINDS", "PROVIDERS", "set_credential"]


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

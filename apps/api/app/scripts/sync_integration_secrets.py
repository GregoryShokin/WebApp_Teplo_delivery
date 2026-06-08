from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models import SourceCredential
from app.services.banking.credentials import set_credential

IIKO_ENV_KEYS = (
    "IIKO_SERVER_BASE_URL",
    "IIKO_SERVER_LOGIN",
    "IIKO_SERVER_PASSWORD",
)


@dataclass(frozen=True)
class CredentialSpec:
    provider: str
    kind: str
    env_name: str


TBANK_BEARER_TOKEN = CredentialSpec("tbank", "bearer_token", "TBANK_API_ACCESS_TOKEN")


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _is_present(name: str) -> bool:
    return bool(_env(name))


def _iiko_is_configured() -> bool:
    return all(_is_present(key) for key in IIKO_ENV_KEYS)


async def _active_credential_values(session: AsyncSession) -> dict[tuple[str, str], str]:
    rows = await session.scalars(
        select(SourceCredential).where(SourceCredential.is_active.is_(True))
    )
    return {
        (row.provider, row.credential_kind): row.value_encrypted
        for row in rows.all()
        if row.provider and row.credential_kind
    }


async def check() -> int:
    async with AsyncSessionLocal() as session:
        active = await _active_credential_values(session)

    iiko_state = "set" if _iiko_is_configured() else "missing"
    env_state = "set" if _env(TBANK_BEARER_TOKEN.env_name) else "missing"
    db_state = (
        "set" if active.get((TBANK_BEARER_TOKEN.provider, TBANK_BEARER_TOKEN.kind)) else "missing"
    )
    print(f"iiko_env={iiko_state}")
    print(f"tbank_bearer_token=env:{env_state} db:{db_state}")
    return 0 if iiko_state == "set" and env_state == "set" and db_state == "set" else 1


async def sync() -> int:
    changed: list[str] = []
    skipped: list[str] = []

    async with AsyncSessionLocal() as session:
        active = await _active_credential_values(session)

    value = _env(TBANK_BEARER_TOKEN.env_name)
    if not value:
        skipped.append(
            f"{TBANK_BEARER_TOKEN.provider}/{TBANK_BEARER_TOKEN.kind}: "
            f"missing {TBANK_BEARER_TOKEN.env_name}"
        )
    elif active.get((TBANK_BEARER_TOKEN.provider, TBANK_BEARER_TOKEN.kind)) == value:
        skipped.append(f"{TBANK_BEARER_TOKEN.provider}/{TBANK_BEARER_TOKEN.kind}: unchanged")
    else:
        async with AsyncSessionLocal() as session:
            await set_credential(
                session,
                provider=TBANK_BEARER_TOKEN.provider,  # type: ignore[arg-type]
                kind=TBANK_BEARER_TOKEN.kind,  # type: ignore[arg-type]
                value=value,
            )
        changed.append(f"{TBANK_BEARER_TOKEN.provider}/{TBANK_BEARER_TOKEN.kind}")

    for item in changed:
        print(f"synced {item}")
    for item in skipped:
        print(f"skipped {item}")
    return await check()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync integration env secrets into app runtime credentials."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only check integration secret availability.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    code = asyncio.run(check() if args.check else sync())
    raise SystemExit(code)


if __name__ == "__main__":
    main()

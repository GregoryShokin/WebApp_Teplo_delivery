from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models import SourceCredential
from app.services.banking.credentials import CredentialKind, Provider, set_credential

IIKO_ENV_KEYS = (
    "IIKO_SERVER_BASE_URL",
    "IIKO_SERVER_LOGIN",
    "IIKO_SERVER_PASSWORD",
)


@dataclass(frozen=True)
class CredentialSpec:
    provider: Provider
    kind: CredentialKind
    env_name: str


@dataclass(frozen=True)
class ActiveCredential:
    value: str
    metadata_json: dict[str, Any] | None


@dataclass(frozen=True)
class CheckStatus:
    iiko_state: str
    tbank_env_state: str
    tbank_db_state: str

    @property
    def exit_code(self) -> int:
        return (
            0
            if self.iiko_state == "set"
            and self.tbank_env_state == "set"
            and self.tbank_db_state == "set"
            else 1
        )


TBANK_BEARER_TOKEN = CredentialSpec("tbank", "bearer_token", "TBANK_API_ACCESS_TOKEN")
TBANK_ACCOUNT_NUMBER_ENV = "TBANK_API_ACCOUNT_NUMBER"

# Sber refreshes OAuth token pairs in the application.  The refresh token and client
# secret must therefore reach the credential store alongside the access token and mTLS
# files. The account number rides as metadata on the access_token credential.
SBER_ACCESS_TOKEN = CredentialSpec("sber", "access_token", "SBER_API_ACCESS_TOKEN")
SBER_REFRESH_TOKEN = CredentialSpec("sber", "refresh_token", "SBER_API_REFRESH_TOKEN")
SBER_CLIENT_SECRET = CredentialSpec("sber", "client_secret", "SBER_API_CLIENT_SECRET")
SBER_MTLS_CERT = CredentialSpec("sber", "mtls_cert_path", "SBER_API_TLS_CERT_PATH")
SBER_MTLS_KEY = CredentialSpec("sber", "mtls_key_path", "SBER_API_TLS_KEY_PATH")
SBER_CREDENTIALS = (
    SBER_ACCESS_TOKEN,
    SBER_REFRESH_TOKEN,
    SBER_CLIENT_SECRET,
    SBER_MTLS_CERT,
    SBER_MTLS_KEY,
)
SBER_ACCOUNT_NUMBER_ENV = "SBER_API_ACCOUNT_NUMBER"


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


async def _active_credentials(session: AsyncSession) -> dict[tuple[str, str], ActiveCredential]:
    rows = await session.scalars(
        select(SourceCredential).where(SourceCredential.is_active.is_(True))
    )
    return {
        (row.provider, row.credential_kind): ActiveCredential(
            value=row.value_encrypted,
            metadata_json=row.metadata_json,
        )
        for row in rows.all()
        if row.provider and row.credential_kind
    }


def _tbank_metadata() -> dict[str, Any] | None:
    account_number = _env(TBANK_ACCOUNT_NUMBER_ENV)
    if not account_number:
        return None
    return {"account_number": account_number}


def _sber_metadata() -> dict[str, Any] | None:
    account_number = _env(SBER_ACCOUNT_NUMBER_ENV)
    if not account_number:
        return None
    return {"account_number": account_number}


async def _update_active_metadata(
    *,
    provider: Provider,
    kind: CredentialKind,
    value: str,
    metadata_json: dict[str, Any] | None,
) -> bool:
    async with AsyncSessionLocal() as session, session.begin():
        credential = await session.scalar(
            select(SourceCredential).where(
                SourceCredential.provider == provider,
                SourceCredential.credential_kind == kind,
                SourceCredential.is_active.is_(True),
            )
        )
        if credential is None or credential.value_encrypted != value:
            return False
        credential.metadata_json = metadata_json
    return True


async def _sync_tbank_bearer_token() -> str:
    async with AsyncSessionLocal() as session:
        active = await _active_credentials(session)

    value = _env(TBANK_BEARER_TOKEN.env_name)
    if not value:
        return "missing"

    metadata_json = _tbank_metadata()
    active_credential = active.get((TBANK_BEARER_TOKEN.provider, TBANK_BEARER_TOKEN.kind))
    if active_credential is None:
        status = "created"
    elif active_credential.value == value:
        if active_credential.metadata_json == metadata_json:
            return "unchanged"
        if await _update_active_metadata(
            provider=TBANK_BEARER_TOKEN.provider,
            kind=TBANK_BEARER_TOKEN.kind,
            value=value,
            metadata_json=metadata_json,
        ):
            return "updated"
        status = "updated"
    else:
        status = "updated"

    async with AsyncSessionLocal() as session:
        await set_credential(
            session,
            provider=TBANK_BEARER_TOKEN.provider,
            kind=TBANK_BEARER_TOKEN.kind,
            value=value,
            metadata_json=metadata_json,
        )
    return status


async def _sync_credential(
    spec: CredentialSpec, metadata_json: dict[str, Any] | None = None
) -> str:
    value = _env(spec.env_name)
    if not value:
        return "missing"

    async with AsyncSessionLocal() as session:
        active = await _active_credentials(session)
    current = active.get((spec.provider, spec.kind))
    if current is None:
        status = "created"
    elif current.value == value:
        if current.metadata_json == metadata_json:
            return "unchanged"
        if await _update_active_metadata(
            provider=spec.provider,
            kind=spec.kind,
            value=value,
            metadata_json=metadata_json,
        ):
            return "updated"
        status = "updated"
    else:
        status = "updated"

    async with AsyncSessionLocal() as session:
        await set_credential(
            session,
            provider=spec.provider,
            kind=spec.kind,
            value=value,
            metadata_json=metadata_json,
        )
    return status


async def _sync_sber_credentials() -> dict[str, str]:
    metadata = _sber_metadata()
    return {
        SBER_ACCESS_TOKEN.kind: await _sync_credential(SBER_ACCESS_TOKEN, metadata),
        SBER_REFRESH_TOKEN.kind: await _sync_credential(SBER_REFRESH_TOKEN),
        SBER_CLIENT_SECRET.kind: await _sync_credential(SBER_CLIENT_SECRET),
        SBER_MTLS_CERT.kind: await _sync_credential(SBER_MTLS_CERT),
        SBER_MTLS_KEY.kind: await _sync_credential(SBER_MTLS_KEY),
    }


def _sber_is_configured() -> bool:
    return all(_is_present(spec.env_name) for spec in SBER_CREDENTIALS)


async def _check_status() -> CheckStatus:
    async with AsyncSessionLocal() as session:
        active = await _active_credential_values(session)

    iiko_state = "set" if _iiko_is_configured() else "missing"
    env_state = "set" if _env(TBANK_BEARER_TOKEN.env_name) else "missing"
    db_state = (
        "set" if active.get((TBANK_BEARER_TOKEN.provider, TBANK_BEARER_TOKEN.kind)) else "missing"
    )
    return CheckStatus(
        iiko_state=iiko_state,
        tbank_env_state=env_state,
        tbank_db_state=db_state,
    )


async def check() -> int:
    status = await _check_status()
    print(f"iiko_env={status.iiko_state}")
    print(f"tbank_bearer_token=env:{status.tbank_env_state} db:{status.tbank_db_state}")
    # SBER выводим, только когда настроен ПОЛНОСТЬЮ (все 3 кредала): частичная настройка
    # бесполезна (mTLS нужен весь), а канал раскатывается «тихо» до готовности.
    if _sber_is_configured():
        async with AsyncSessionLocal() as session:
            active = await _active_credential_values(session)
        for spec in SBER_CREDENTIALS:
            env_state = "set" if _env(spec.env_name) else "missing"
            db_state = "set" if active.get((spec.provider, spec.kind)) else "missing"
            print(f"sber_{spec.kind}=env:{env_state} db:{db_state}")
    return status.exit_code


async def sync() -> int:
    print(f"iiko_env={'set' if _iiko_is_configured() else 'missing'}")
    print(f"tbank_bearer_token={await _sync_tbank_bearer_token()}")
    # SBER синкаем/печатаем только при полной настройке (см. check()).
    if _sber_is_configured():
        for kind, kind_status in (await _sync_sber_credentials()).items():
            print(f"sber_{kind}={kind_status}")
    return (await _check_status()).exit_code


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

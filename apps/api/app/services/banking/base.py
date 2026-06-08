from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models import Account, SourceCredential
from app.services.banking.exceptions import BankCredentialsError

API_DIR = Path(__file__).resolve().parents[3]
BANK_FIXTURES_DIR = API_DIR / "tests" / "fixtures" / "bank"


class NormalizedBankOperation(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    provider: str
    provider_operation_id: str
    account_number: str
    operation_date: date
    posted_at: datetime | None = None
    direction: str = Field(pattern="^(in|out)$")
    amount: Decimal
    currency: str = "RUB"
    counterparty_name_raw: str | None = None
    counterparty_inn_raw: str | None = None
    counterparty_account_raw: str | None = None
    payment_purpose: str | None = None
    document_number: str | None = None
    raw_payload: dict[str, Any]


class AccountMeta(BaseModel):
    account_number: str
    bic: str | None = None
    currency: str = "RUB"
    legal_entity_inn: str | None = None


class PaymentDraftResult(BaseModel):
    document_id: str
    status: str
    provider_ref: str | None = None


class BankClient(Protocol):
    provider: str

    async def fetch_statement(
        self, *, date_from: date, date_to: date
    ) -> list[NormalizedBankOperation]: ...

    async def fetch_account_metadata(self) -> list[AccountMeta]: ...

    async def create_payment_draft(
        self,
        *,
        document_id: str,
        amount: Decimal,
        purpose: str,
        requisites: dict[str, Any],
        payer_account: str,
    ) -> PaymentDraftResult: ...


def settings() -> Settings:
    return get_settings()


def date_range(date_from: date, date_to: date) -> Iterable[date]:
    if date_to < date_from:
        raise ValueError("date_to must be greater than or equal to date_from")
    for offset in range((date_to - date_from).days + 1):
        yield date_from + timedelta(days=offset)


def money(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return abs(value)
    if isinstance(value, dict):
        for key in ("amount", "value", "sum", "rub", "minorUnits"):
            if key in value:
                return money(value.get(key))
        units = value.get("units")
        nano = value.get("nano")
        if units is not None or nano is not None:
            parsed = money(units or 0) + (money(nano or 0) / Decimal("1000000000"))
            return abs(parsed)
    if value is None or value == "":
        return Decimal("0")
    text = str(value).replace(" ", "").replace(",", ".")
    text = re.sub(r"[^0-9.\-]", "", text)
    if text.count(".") > 1:
        parts = text.split(".")
        text = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return abs(Decimal(text))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def clean_digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def scalar(row: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = row.get(name)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return str(value)
    return ""


def nested_scalar(row: dict[str, Any], block_name: str, names: tuple[str, ...]) -> str:
    block = row.get(block_name)
    if not isinstance(block, dict):
        return ""
    return scalar(block, names)


def parse_date(value: Any, fallback: date) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value or "")
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if not match:
        return fallback
    try:
        return datetime.strptime(match.group(0), "%Y-%m-%d").date()
    except ValueError:
        return fallback


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.combine(
                datetime.strptime(text[:10], "%Y-%m-%d").date(), time.min, tzinfo=UTC
            )
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def read_fixture(provider: str, filename: str) -> Any:
    path = BANK_FIXTURES_DIR / provider / filename
    return json.loads(path.read_text(encoding="utf-8"))


def operation_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    candidates = [
        payload.get("transactions"),
        payload.get("operations"),
        payload.get("items"),
        payload.get("data"),
        payload.get("payload"),
    ]
    for value in candidates:
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


async def active_credential(
    session: AsyncSession | None, provider: str, kind: str
) -> SourceCredential | None:
    if session is None:
        return None
    return await session.scalar(
        select(SourceCredential).where(
            SourceCredential.provider == provider,
            SourceCredential.credential_kind == kind,
            SourceCredential.is_active.is_(True),
        )
    )


async def required_credential(session: AsyncSession | None, provider: str, kind: str) -> str:
    credential = await active_credential(session, provider, kind)
    if credential is None or not credential.value_encrypted:
        raise BankCredentialsError(provider, f"missing active credential {kind}")
    return credential.value_encrypted


async def configured_account_metadata(
    session: AsyncSession | None, provider: str, fallback_account_number: str | None = None
) -> list[AccountMeta]:
    rows: list[AccountMeta] = []
    if session is not None:
        accounts = await session.scalars(
            select(Account).where(
                Account.bank_code == provider,
                Account.account_number.is_not(None),
                Account.status == "active",
            )
        )
        for account in accounts.all():
            if account.account_number:
                rows.append(
                    AccountMeta(
                        account_number=account.account_number,
                        bic=account.bic,
                        currency=account.currency,
                    )
                )
        credentials = await session.scalars(
            select(SourceCredential).where(
                SourceCredential.provider == provider,
                SourceCredential.is_active.is_(True),
            )
        )
        for credential in credentials.all():
            rows.extend(_account_meta_from_mapping(credential.metadata_json or {}))
    if fallback_account_number:
        rows.append(AccountMeta(account_number=fallback_account_number))
    return dedupe_account_meta(rows)


def dedupe_account_meta(rows: Iterable[AccountMeta]) -> list[AccountMeta]:
    seen: set[str] = set()
    result: list[AccountMeta] = []
    for row in rows:
        number = clean_digits(row.account_number)
        if not number or number in seen:
            continue
        seen.add(number)
        result.append(row.model_copy(update={"account_number": number}))
    return result


def _account_meta_from_mapping(payload: dict[str, Any]) -> list[AccountMeta]:
    rows: list[AccountMeta] = []
    accounts = payload.get("accounts")
    if isinstance(accounts, list):
        for item in accounts:
            if not isinstance(item, dict):
                continue
            account_number = clean_digits(
                item.get("account_number") or item.get("accountNumber") or item.get("acct")
            )
            if not account_number:
                continue
            rows.append(
                AccountMeta(
                    account_number=account_number,
                    bic=str(item.get("bic") or item.get("bank_bic") or item.get("bicRu") or "")
                    or None,
                    currency=str(item.get("currency") or "RUB"),
                    legal_entity_inn=clean_digits(
                        item.get("legal_entity_inn") or item.get("legalEntityInn")
                    )
                    or None,
                )
            )
    account_number = clean_digits(payload.get("account_number") or payload.get("accountNumber"))
    if account_number:
        rows.append(
            AccountMeta(
                account_number=account_number,
                bic=str(payload.get("bic") or payload.get("bank_bic") or "") or None,
                currency=str(payload.get("currency") or "RUB"),
                legal_entity_inn=clean_digits(
                    payload.get("legal_entity_inn") or payload.get("legalEntityInn")
                )
                or None,
            )
        )
    return rows

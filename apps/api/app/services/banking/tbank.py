from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.services.banking.base import (
    AccountMeta,
    NormalizedBankOperation,
    clean_digits,
    configured_account_metadata,
    date_range,
    money,
    nested_scalar,
    operation_rows,
    parse_date,
    parse_datetime,
    read_fixture,
    required_credential,
    scalar,
)
from app.services.banking.exceptions import BankCredentialsError, BankFetchError


class TbankClient:
    provider = "tbank"

    def __init__(
        self, session: AsyncSession | None = None, settings: Settings | None = None
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()

    async def fetch_account_metadata(self) -> list[AccountMeta]:
        if self.settings.teplo_bank_client_mode == "mock":
            payload = read_fixture(self.provider, "accounts.json")
            return [
                AccountMeta(
                    account_number=clean_digits(
                        row.get("account_number") or row.get("accountNumber")
                    ),
                    bic=str(row.get("bic") or "") or None,
                    currency=str(row.get("currency") or "RUB"),
                    legal_entity_inn=clean_digits(
                        row.get("legal_entity_inn") or row.get("legalEntityInn")
                    )
                    or None,
                )
                for row in payload
                if isinstance(row, dict)
            ]
        return await configured_account_metadata(
            self.session, self.provider, self.settings.tbank_api_account_number
        )

    async def fetch_statement(
        self, *, date_from: date, date_to: date
    ) -> list[NormalizedBankOperation]:
        if self.settings.teplo_bank_client_mode == "mock":
            return await self._fetch_mock_statement(date_from=date_from, date_to=date_to)
        return await self._fetch_live_statement(date_from=date_from, date_to=date_to)

    async def _fetch_mock_statement(
        self, *, date_from: date, date_to: date
    ) -> list[NormalizedBankOperation]:
        metadata = await self.fetch_account_metadata()
        fallback_account = metadata[0].account_number if metadata else ""
        operations: list[NormalizedBankOperation] = []
        for day in date_range(date_from, date_to):
            try:
                payload = read_fixture(self.provider, f"statement_{day.isoformat()}.json")
            except FileNotFoundError:
                continue
            for row in operation_rows(payload):
                account_number = clean_digits(
                    row.get("accountNumber") or row.get("account_number") or fallback_account
                )
                operations.append(self._normalize_operation(row, account_number, day))
        return operations

    async def _fetch_live_statement(
        self, *, date_from: date, date_to: date
    ) -> list[NormalizedBankOperation]:
        account_metadata = await self.fetch_account_metadata()
        account_numbers = [row.account_number for row in account_metadata]
        if not account_numbers:
            raise BankFetchError(self.provider, "no T-Bank account numbers configured")

        token = await required_credential(self.session, self.provider, "bearer_token")
        operations: list[NormalizedBankOperation] = []
        async with httpx.AsyncClient(
            base_url=self.settings.tbank_api_base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=self.settings.bank_client_timeout_seconds,
        ) as client:
            for account_number in account_numbers:
                cursor = ""
                while True:
                    params: dict[str, Any] = {
                        "accountNumber": account_number,
                        "from": date_from.isoformat(),
                        "to": date_to.isoformat(),
                        "limit": 1000,
                    }
                    if cursor:
                        params["cursor"] = cursor
                    payload = await self._get_json(client, "/api/v1/statement", params)
                    rows = operation_rows(payload)
                    operations.extend(
                        self._normalize_operation(row, account_number, date_from) for row in rows
                    )
                    cursor = _next_cursor(payload)
                    if not cursor:
                        break
        return operations

    async def _get_json(
        self, client: httpx.AsyncClient, path: str, params: dict[str, Any]
    ) -> Any:
        response = await client.get(
            path,
            params=params,
            headers={"X-Request-Id": str(uuid.uuid4())},
        )
        if response.status_code in {401, 403}:
            raise BankCredentialsError(self.provider, "T-Bank bearer token is invalid or expired")
        if response.status_code >= 400:
            raise BankFetchError(self.provider, f"T-Bank API returned {response.status_code}")
        return response.json()

    def _normalize_operation(
        self, row: dict[str, Any], account_number: str, fallback_day: date
    ) -> NormalizedBankOperation:
        direction = _direction(row)
        block = _counterparty_block(row, direction)
        counterparty_name = _block_value(
            block, ("name", "fullName", "shortName", "counterpartyName")
        )
        counterparty_inn = _block_value(block, ("inn", "INN", "taxId", "tin"))
        counterparty_account = _block_value(
            block, ("acct", "account", "accountNumber", "bankAccount")
        )
        if not (counterparty_name or counterparty_inn or counterparty_account):
            counterparty_name = nested_scalar(
                row, "counterparty", ("name", "fullName", "shortName")
            ) or scalar(row, ("counterpartyName", "counteragentName"))
            counterparty_inn = nested_scalar(
                row, "counterparty", ("inn", "taxId", "tin")
            ) or scalar(row, ("counterpartyInn", "inn"))
            counterparty_account = nested_scalar(
                row, "counterparty", ("acct", "account", "accountNumber", "bankAccount")
            ) or scalar(row, ("counterpartyAccount",))

        raw_payload = dict(row)
        raw_payload.setdefault("accountNumber", account_number)
        provider_operation_id = (
            row.get("operationId")
            or row.get("id")
            or row.get("ucid")
            or row.get("documentNumber")
            or (
                f"{account_number}:"
                f"{parse_date(row.get('operationDate'), fallback_day).isoformat()}:"
                f"{direction}:{money(row.get('amount'))}"
            )
        )
        return NormalizedBankOperation(
            provider=self.provider,
            provider_operation_id=str(provider_operation_id),
            account_number=account_number,
            operation_date=parse_date(
                row.get("operationDate")
                or row.get("transactionDate")
                or row.get("date")
                or row.get("documentDate"),
                fallback_day,
            ),
            posted_at=parse_datetime(
                row.get("operationDate")
                or row.get("authorizationDate")
                or row.get("trxnPostDate")
                or row.get("createdAt")
            ),
            direction=direction,
            amount=money(row.get("amount") or row.get("operationAmount") or row.get("sum")),
            currency=str(row.get("currency") or row.get("operationCurrency") or "RUB"),
            counterparty_name_raw=counterparty_name or None,
            counterparty_inn_raw=clean_digits(counterparty_inn) or None,
            counterparty_account_raw=clean_digits(counterparty_account) or None,
            payment_purpose=scalar(
                row, ("paymentPurpose", "purpose", "description", "operationDescription")
            )
            or None,
            document_number=scalar(row, ("documentNumber", "docNumber", "number")) or None,
            raw_payload=raw_payload,
        )


def _direction(row: dict[str, Any]) -> str:
    value = scalar(
        row,
        ("direction", "typeOfOperation", "operationType", "type", "movementType"),
    ).casefold()
    if value in {"debit", "out", "outcome", "expense"} or "debit" in value or "out" in value:
        return "out"
    if value in {"credit", "in", "income", "receipt"} or "credit" in value or "income" in value:
        return "in"
    signed_amount = money(row.get("amount") or row.get("operationAmount") or row.get("sum"))
    return "out" if signed_amount < 0 else "in"


def _counterparty_block(row: dict[str, Any], direction: str) -> dict[str, Any]:
    payer = row.get("payer") if isinstance(row.get("payer"), dict) else {}
    receiver = row.get("receiver") if isinstance(row.get("receiver"), dict) else {}
    if direction == "out":
        return receiver
    return payer


def _block_value(block: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = block.get(name)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return str(value)
    return ""


def _next_cursor(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("nextCursor", "next_cursor"):
        if payload.get(key):
            return str(payload[key])
    for block_name in ("paging", "page", "result", "data"):
        block = payload.get(block_name)
        if isinstance(block, dict) and block.get("nextCursor"):
            return str(block["nextCursor"])
    return ""

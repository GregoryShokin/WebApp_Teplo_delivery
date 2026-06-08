from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.services.banking.base import (
    AccountMeta,
    NormalizedBankOperation,
    PaymentDraftResult,
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

PAYMENT_DRAFT_PATH = "/api/v1/payment/create"
DEFAULT_TAX_FIELDS = {
    "taxPayerStatus": "0",
    "kbk": "0",
    "oktmo": "0",
    "taxEvidence": "0",
    "taxPeriod": "0",
    "uin": "0",
    "taxDocNumber": "0",
    "taxDocDate": "0",
}


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

    async def create_payment_draft(
        self,
        *,
        document_id: str,
        amount: Decimal,
        purpose: str,
        requisites: dict[str, Any],
        payer_account: str,
    ) -> PaymentDraftResult:
        if self.settings.teplo_bank_client_mode == "mock":
            return PaymentDraftResult(
                document_id=document_id,
                status="created",
                provider_ref=f"mock-{document_id}",
            )

        token = await required_credential(self.session, self.provider, "bearer_token")
        payload = build_payment_draft_api_payload(
            document_id=document_id,
            amount=amount,
            purpose=purpose,
            requisites=requisites,
            payer_account=payer_account,
        )
        async with httpx.AsyncClient(
            base_url=self.settings.tbank_payment_base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=self.settings.bank_client_timeout_seconds,
        ) as client:
            response = await client.post(
                PAYMENT_DRAFT_PATH,
                json=payload,
                headers={"X-Request-Id": str(uuid.uuid4())},
            )
        if response.status_code in {401, 403}:
            raise BankCredentialsError(self.provider, "T-Bank bearer token is invalid or expired")
        if response.status_code >= 400:
            raise BankFetchError(self.provider, f"T-Bank API returned {response.status_code}")

        try:
            response_payload = response.json()
        except ValueError:
            response_payload = {}
        provider_document_id = _first_scalar(
            response_payload,
            ("documentId", "document_id", "id"),
        )
        status = _first_scalar(
            response_payload,
            ("status", "documentStatus", "paymentStatus"),
        )
        return PaymentDraftResult(
            document_id=document_id,
            status=status or "created",
            provider_ref=provider_document_id or None,
        )

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

    async def _get_json(self, client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> Any:
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


def build_payment_draft_api_payload(
    *,
    document_id: str,
    amount: Decimal,
    purpose: str,
    requisites: Mapping[str, Any],
    payer_account: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "documentNumber": _document_number(document_id),
        "amount": _json_amount(amount),
        "recipientName": _required_requisite(requisites, "recipientName", "recipient_name"),
        "inn": clean_digits(_required_requisite(requisites, "inn", "recipientInn")),
        "kpp": clean_digits(_requisite(requisites, "kpp", "recipientKpp") or "0"),
        "bankAcnt": clean_digits(_required_requisite(requisites, "bankAcnt", "bank_acnt")),
        "bankBik": clean_digits(_required_requisite(requisites, "bankBik", "bank_bik", "bik")),
        "accountNumber": clean_digits(payer_account),
        "paymentPurpose": " ".join(str(purpose).split()),
        "executionOrder": int(_requisite(requisites, "executionOrder", "execution_order") or 5),
        "recipientCorrAccountNumber": clean_digits(
            _required_requisite(
                requisites,
                "recipientCorrAccountNumber",
                "corrAccount",
                "corr_account",
                "correspondent_account",
            )
        ),
    }
    payload.update(DEFAULT_TAX_FIELDS)
    _validate_payment_draft_payload(payload)
    return payload


def _document_number(document_id: str) -> str:
    digits = clean_digits(document_id)
    if digits:
        number = int(digits[-6:])
    else:
        number = int(hashlib.sha256(document_id.encode("utf-8")).hexdigest()[:12], 16)
    number = number % 999999
    if number == 0:
        number = 1
    return str(number)


def _json_amount(value: Any) -> int | float:
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount == amount.to_integral_value():
        return int(amount)
    return float(amount)


def _requisite(requisites: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = requisites.get(name)
        if value not in (None, ""):
            return value
    return None


def _required_requisite(requisites: Mapping[str, Any], *names: str) -> str:
    value = _requisite(requisites, *names)
    if value in (None, ""):
        raise ValueError(f"missing payment requisite: {names[0]}")
    return str(value)


def _validate_payment_draft_payload(payload: Mapping[str, Any]) -> None:
    if Decimal(str(payload["amount"])) <= 0:
        raise ValueError("payment amount must be greater than zero")
    if not payload["accountNumber"]:
        raise ValueError("payer account is required")
    if len(str(payload["paymentPurpose"])) > 210:
        raise ValueError("payment purpose must be 210 characters or fewer")
    execution_order = payload["executionOrder"]
    if not isinstance(execution_order, int) or execution_order < 1 or execution_order > 5:
        raise ValueError("executionOrder must be an integer from 1 to 5")


def _first_scalar(payload: Any, names: tuple[str, ...]) -> str:
    if isinstance(payload, dict):
        for name in names:
            value = payload.get(name)
            if value not in (None, "") and not isinstance(value, (dict, list)):
                return str(value)
        for value in payload.values():
            nested = _first_scalar(value, names)
            if nested:
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = _first_scalar(item, names)
            if nested:
                return nested
    return ""

from __future__ import annotations

import ssl
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
    operation_rows,
    parse_date,
    parse_datetime,
    read_fixture,
    required_credential,
)
from app.services.banking.exceptions import BankCredentialsError, BankFetchError


class SberClient:
    provider = "sber"

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
            self.session, self.provider, self.settings.sber_api_account_number
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
                operations.append(self._normalize_transaction(row, account_number, day))
        return operations

    async def _fetch_live_statement(
        self, *, date_from: date, date_to: date
    ) -> list[NormalizedBankOperation]:
        account_metadata = await self.fetch_account_metadata()
        account_numbers = [row.account_number for row in account_metadata]
        if not account_numbers:
            raise BankFetchError(self.provider, "no Sber account numbers configured")

        token = await required_credential(self.session, self.provider, "access_token")
        cert_path = await required_credential(self.session, self.provider, "mtls_cert_path")
        key_path = await required_credential(self.session, self.provider, "mtls_key_path")

        # httpx 0.28 dropped the `cert=(certfile, keyfile)` argument: passing it is
        # silently ignored, so the client certificate is never presented and Sber's
        # gateway answers "400 No required SSL certificate was sent". Build the mTLS
        # context explicitly and hand it to httpx via `verify=`.
        ssl_context = ssl.create_default_context(
            cafile=self.settings.sber_api_ca_bundle_path or None
        )
        ssl_context.load_cert_chain(certfile=cert_path, keyfile=key_path)

        operations: list[NormalizedBankOperation] = []
        async with httpx.AsyncClient(
            base_url=self.settings.sber_api_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            verify=ssl_context,
            timeout=self.settings.bank_client_timeout_seconds,
        ) as client:
            for account_number in account_numbers:
                for day in date_range(date_from, date_to):
                    await self._get_json(
                        client,
                        "/v2/statement/summary",
                        {"accountNumber": account_number, "statementDate": day.isoformat()},
                    )
                    page = 1
                    while True:
                        payload = await self._get_json(
                            client,
                            "/v2/statement/transactions",
                            {
                                "accountNumber": account_number,
                                "statementDate": day.isoformat(),
                                "page": page,
                                "size": 100,
                            },
                        )
                        rows = operation_rows(payload)
                        if not rows:
                            break
                        operations.extend(
                            self._normalize_transaction(row, account_number, day) for row in rows
                        )
                        if len(rows) < 100 and not _has_next_page(payload):
                            break
                        page += 1
        return operations

    async def _get_json(self, client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> Any:
        response = await client.get(path, params=params)
        if response.status_code == 401:
            raise BankCredentialsError(self.provider, "Sber access token is invalid or expired")
        if response.status_code >= 400:
            raise BankFetchError(self.provider, f"Sber API returned {response.status_code}")
        return response.json()

    def _normalize_transaction(
        self, row: dict[str, Any], account_number: str, fallback_day: date
    ) -> NormalizedBankOperation:
        raw_direction = str(row.get("direction") or "").upper()
        direction = "in" if raw_direction in {"CREDIT", "IN", "INCOME"} else "out"
        transfer = row.get("rurTransfer") if isinstance(row.get("rurTransfer"), dict) else {}
        if direction == "in":
            counterparty_name = transfer.get("payerName") or row.get("payerName")
            counterparty_inn = transfer.get("payerInn") or row.get("payerInn")
            counterparty_account = (
                transfer.get("payerAccount")
                or row.get("payerAccount")
                or row.get("correspondingAccount")
            )
        else:
            counterparty_name = transfer.get("payeeName") or row.get("payeeName")
            counterparty_inn = transfer.get("payeeInn") or row.get("payeeInn")
            counterparty_account = (
                transfer.get("payeeAccount")
                or row.get("payeeAccount")
                or row.get("correspondingAccount")
            )

        raw_payload = dict(row)
        raw_payload.setdefault("accountNumber", account_number)
        provider_operation_id = (
            row.get("operationId")
            or row.get("transactionId")
            or row.get("id")
            or row.get("documentNumber")
            or (
                f"{account_number}:{fallback_day.isoformat()}:{direction}:"
                f"{money(row.get('amountRub') or row.get('amount'))}"
            )
        )
        return NormalizedBankOperation(
            provider=self.provider,
            provider_operation_id=str(provider_operation_id),
            account_number=account_number,
            operation_date=parse_date(
                row.get("operationDate") or row.get("documentDate"), fallback_day
            ),
            posted_at=parse_datetime(
                row.get("composedDateTime")
                or row.get("lastModifiedTime")
                or row.get("receiptDate")
                or row.get("operationDate")
            ),
            direction=direction,
            amount=money(row.get("amountRub") or row.get("amount")),
            currency=str(row.get("currency") or row.get("currencyCode") or "RUB"),
            counterparty_name_raw=str(counterparty_name or "") or None,
            counterparty_inn_raw=clean_digits(counterparty_inn) or None,
            counterparty_account_raw=clean_digits(counterparty_account) or None,
            payment_purpose=str(
                row.get("paymentPurpose")
                or transfer.get("paymentPurpose")
                or row.get("purpose")
                or ""
            )
            or None,
            document_number=str(row.get("documentNumber") or "") or None,
            raw_payload=raw_payload,
        )


def _has_next_page(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    links = payload.get("_links")
    if isinstance(links, dict):
        return bool(links.get("next"))
    if isinstance(links, list):
        return any(
            isinstance(link, dict)
            and str(link.get("rel") or link.get("name") or "").casefold() == "next"
            for link in links
        )
    return False

from __future__ import annotations

import ssl
import uuid
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select
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
    operation_rows,
    parse_date,
    parse_datetime,
    read_fixture,
    required_credential,
)
from app.services.banking.exceptions import BankCredentialsError, BankFetchError

# Сбер Fintech API создания рублёвого платёжного поручения (РПП). Без блока `digestSignatures`
# документ создаётся в статусе ЧЕРНОВИК (bankStatus=CREATED) — оператор подписывает его в
# СберБизнес. Это зеркало T-Bank-черновика. Scope токена: PAY_DOC_RU; контур — тот же mTLS,
# что и выписка (`/run/secrets/teplo/sber/{ca,client.crt,client.key}`).
PAYMENT_CREATE_PATH = "/v1/payments"
SBER_PAYER_REQUISITES_KEY = "payroll.sber_payer_requisites"
MOCK_PAYER_ACCOUNT = "00000000000000000000"

# bankStatus из `GET /v1/payments/{externalId}/state`.
_SBER_STATUS_PAID = frozenset({"IMPLEMENTED"})
_SBER_STATUS_FAILED = frozenset(
    {
        "DELETED",
        "INVALIDEDS",
        "RECALL",
        "REFUSEDBYBANK",
        "REFUSEDBYABS",
        "REQUISITEERROR",
        "REFUSED_BY_RZK",
        "FRAUDDENY",
    }
)


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

    async def create_payment_draft(
        self,
        *,
        document_id: str,
        amount: Decimal,
        purpose: str,
        requisites: dict[str, Any],
        payer_account: str,
    ) -> PaymentDraftResult:
        """Создать рублёвый РПП в Сбере в статусе ЧЕРНОВИК (без ЭП) — оператор подписывает его
        в СберБизнес. Полное зеркало ``TbankClient.create_payment_draft`` (тот же интерфейс
        Protocol ``BankClient``), но через Сбер Fintech API `POST /v1/payments` (scope
        PAY_DOC_RU, mTLS). ``requisites`` — реквизиты ПОЛУЧАТЕЛЯ (Сейф,
        ``payroll.bank_payout_requisites``); блок ПЛАТЕЛЬЩИКА берётся из
        ``payroll.sber_payer_requisites``. ``provider_ref`` = наш ``externalId`` (по нему потом
        читаем статус). В mock банк не вызывается."""
        external_id = _sber_external_id(document_id)
        if self.settings.teplo_bank_client_mode == "mock":
            return PaymentDraftResult(
                document_id=document_id, status="created", provider_ref=external_id
            )

        payer = await self._payer_requisites()
        payload = build_sber_payment_payload(
            external_id=external_id,
            amount=amount,
            purpose=purpose,
            payee=requisites,
            payer=payer,
            payer_account=payer_account,
        )
        async with await self._authorized_client() as client:
            response = await client.post(
                PAYMENT_CREATE_PATH,
                json=payload,
                headers={"X-Request-Id": str(uuid.uuid4())},
            )
        if response.status_code in {401, 403}:
            raise BankCredentialsError(self.provider, "Sber access token/scope is invalid")
        if response.status_code >= 400:
            raise BankFetchError(self.provider, f"Sber API returned {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        bank_status = data.get("bankStatus")
        returned_id = data.get("externalId") or external_id
        return PaymentDraftResult(
            document_id=document_id,
            status=str(bank_status).lower() if bank_status else "created",
            provider_ref=str(returned_id),
        )

    async def get_payment_status(self, payment_id: str) -> str | None:
        """Статус Сбер-РПП по ``externalId`` — `GET /v1/payments/{externalId}/state`.

        Нормализует ``bankStatus`` в ``paid`` (IMPLEMENTED) / ``failed`` (отказ/удалён) /
        ``pending`` (в процессе). У Сбера НЕТ вебхука, поэтому доводка статуса идёт поллингом.
        В mock статуса нет (None)."""
        if not payment_id or self.settings.teplo_bank_client_mode == "mock":
            return None
        async with await self._authorized_client() as client:
            response = await client.get(
                f"{PAYMENT_CREATE_PATH}/{payment_id}/state",
                headers={"X-Request-Id": str(uuid.uuid4())},
            )
        if response.status_code in {401, 403}:
            raise BankCredentialsError(self.provider, "Sber access token/scope is invalid")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise BankFetchError(self.provider, f"Sber API returned {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            return None
        bank_status = str(data.get("bankStatus") or "").upper() if isinstance(data, dict) else ""
        if bank_status in _SBER_STATUS_PAID:
            return "paid"
        if bank_status in _SBER_STATUS_FAILED:
            return "failed"
        return "pending"

    async def _authorized_client(self) -> httpx.AsyncClient:
        """httpx-клиент Сбера с Bearer-токеном и клиентским mTLS (обязателен для gateway'я).

        httpx 0.28 игнорирует ``cert=(certfile, keyfile)`` — сертификат не предъявляется и Сбер
        отвечает «400 No required SSL certificate was sent». Поэтому строим mTLS-контекст явно
        и отдаём через ``verify=``. Тот же контур, что и выписка."""
        token = await required_credential(self.session, self.provider, "access_token")
        cert_path = await required_credential(self.session, self.provider, "mtls_cert_path")
        key_path = await required_credential(self.session, self.provider, "mtls_key_path")
        ssl_context = ssl.create_default_context(
            cafile=self.settings.sber_api_ca_bundle_path or None
        )
        ssl_context.load_cert_chain(certfile=cert_path, keyfile=key_path)
        return httpx.AsyncClient(
            base_url=self.settings.sber_api_base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            verify=ssl_context,
            timeout=self.settings.bank_client_timeout_seconds,
        )

    async def _payer_requisites(self) -> Mapping[str, Any]:
        from app.models import AppSetting

        setting = await self.session.scalar(
            select(AppSetting).where(AppSetting.key == SBER_PAYER_REQUISITES_KEY)
        )
        if setting is None or not isinstance(setting.value, Mapping):
            raise BankFetchError(
                self.provider,
                f"не настроены реквизиты плательщика Сбера ({SBER_PAYER_REQUISITES_KEY})",
            )
        return setting.value

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

        operations: list[NormalizedBankOperation] = []
        async with await self._authorized_client() as client:
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


def _sber_external_id(document_id: str) -> str:
    """Детерминированный UUID (``externalId`` Сбера) из нашего ``document_id`` — стабилен при
    повторе, поэтому повторная отправка не плодит дубли и статус читается по нему же."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"teplo-sber-payment:{document_id}"))


def _sber_amount(value: Any) -> float | int:
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    return int(amount) if amount == amount.to_integral_value() else float(amount)


def _sber_requisite(src: Mapping[str, Any], *keys: str, required: bool = False) -> str:
    for key in keys:
        value = src.get(key)
        if value not in (None, ""):
            return str(value)
    if required:
        raise ValueError(f"Sber payload: не хватает поля {keys[0]}")
    return ""


def build_sber_payment_payload(
    *,
    external_id: str,
    amount: Decimal,
    purpose: str,
    payee: Mapping[str, Any],
    payer: Mapping[str, Any],
    payer_account: str,
    doc_date: date | None = None,
) -> dict[str, Any]:
    """Тело `POST /v1/payments` (рублёвый РПП). БЕЗ блока ``digestSignatures`` → банк создаёт
    документ в статусе ЧЕРНОВИК, оператор подписывает его в СберБизнес.

    ``payer`` — наш Сбер-счёт (``payroll.sber_payer_requisites``); ``payee`` — получатель-Сейф
    (``payroll.bank_payout_requisites``, ключи как у T-Bank payload: recipientName/inn/kpp/
    bankAcnt/bankBik/recipientCorrAccountNumber).

    ⚠️ Набор и имена полей (VAT-блок, формат ``date``) сверить с рабочим PoC-запросом перед
    боевым запуском — Сбер строг к валидации; здесь реализована версия по офиц. спецификации."""
    return {
        "externalId": external_id,
        "date": (doc_date or datetime.now().date()).isoformat(),
        "amount": _sber_amount(amount),
        "operationCode": "01",  # рублёвый платёж (обязателен по схеме Сбера)
        "priority": str(_sber_requisite(payee, "priority", "executionOrder") or "5"),
        "purpose": " ".join(str(purpose).split()),
        # Плательщик — наш расчётный счёт в Сбере (блок обязателен, не автозаполняется).
        "payerName": _sber_requisite(payer, "payerName", "recipientName", "name", required=True),
        "payerInn": clean_digits(_sber_requisite(payer, "payerInn", "inn", required=True)),
        "payerKpp": clean_digits(_sber_requisite(payer, "payerKpp", "kpp") or "0"),
        "payerAccount": clean_digits(
            _sber_requisite(payer, "payerAccount", "bankAcnt") or payer_account
        ),
        "payerBankBic": clean_digits(
            _sber_requisite(payer, "payerBankBic", "bankBik", "bik", required=True)
        ),
        "payerBankCorrAccount": clean_digits(
            _sber_requisite(
                payer, "payerBankCorrAccount", "corrAccount", "recipientCorrAccountNumber"
            )
        ),
        # Получатель — Сейф (Шокина К.Ю.).
        "payeeName": _sber_requisite(payee, "recipientName", "payeeName", "name", required=True),
        "payeeInn": clean_digits(
            _sber_requisite(payee, "inn", "payeeInn", "recipientInn", required=True)
        ),
        "payeeKpp": clean_digits(_sber_requisite(payee, "kpp", "payeeKpp") or "0"),
        "payeeAccount": clean_digits(
            _sber_requisite(payee, "bankAcnt", "payeeAccount", required=True)
        ),
        "payeeBankBic": clean_digits(
            _sber_requisite(payee, "bankBik", "bik", "payeeBankBic", required=True)
        ),
        "payeeBankCorrAccount": clean_digits(
            _sber_requisite(
                payee, "recipientCorrAccountNumber", "corrAccount", "payeeBankCorrAccount"
            )
        ),
    }


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

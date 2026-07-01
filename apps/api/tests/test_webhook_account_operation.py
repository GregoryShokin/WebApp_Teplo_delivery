"""Вебхук «Операция по счёту» T-Банка: подтверждается, но в ДДС НЕ пишется.

Оплату банковских черновиков фиксирует статусный webhook платёжного документа; операции по
счёту (строки выписки) на этот контур приходят, но игнорируются — не создают BankOperation,
чтобы выписка не задваивала проводку поверх статусной оплаты. Покрывает: ack без ингеста
(ingested=false, reason=account_operation_ignored), стадия hold/transaction, 422 без
operationId, приём того же тела на /payment-status, авторизацию токеном.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.models import BankOperation

BASE = "/api/v1/webhooks/tbank/account-operation"
PAYMENT_STATUS = "/api/v1/webhooks/tbank/payment-status"


def _run(coro):
    return asyncio.run(coro)


def _credit_body(operation_id: str, *, amount: str = "50000", status: str = "Transaction") -> dict:
    # Формат реальной строки выписки T-Банка (вебхук «Операция по счёту» шлёт её же).
    return {
        "operationId": operation_id,
        "typeOfOperation": "Credit",
        "accountNumber": "40802810000000012345",
        "documentNumber": "22",
        "operationAmount": amount,
        "accountAmount": amount,
        "rubleAmount": amount,
        "accountCurrencyDigitalCode": "643",
        "operationStatus": status,
        "operationDate": "2026-06-23T11:48:06Z",
        "trxnPostDate": "2026-06-23T11:48:06Z",
        "authorizationDate": "2026-06-23T11:48:06Z",
        "payPurpose": "Отражение операции оплаты по договору 22",
        "description": "Оплата по договору 22",
        "payer": {
            "account": "40702810900000099999",
            "name": 'ООО "Контрагент"',
            "inn": "7700000000",
        },
        "receiver": {"account": "40802810000000012345", "name": "ИП Шокина Е.А."},
    }


async def _count(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(BankOperation)
                .where(BankOperation.provider == "tbank")
            )
            or 0
        )


def test_account_operation_acked_but_not_ingested(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    op_id = "126cc5a2-41ca-0083-9017-5863a14692df"
    resp = client.post(BASE, json=_credit_body(op_id))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ingested"] is False and body["skipped"] is True
    assert body["reason"] == "account_operation_ignored"
    assert body["stage"] == "transaction"
    assert body["inserted"] == 0 and body["updated"] == 0
    # Операция по счёту в ДДС НЕ попадает — журнал пуст.
    assert _run(_count(async_session_factory)) == 0


def test_payment_status_route_ignores_account_operation(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Банк по заявке может слать операции по счёту на /tbank/payment-status — этот URL их тоже
    игнорирует (не путает с реальным статусом платёжного документа), в ДДС не пишет."""
    op_id = "7ea7de7e-91b3-0059-a742-59a5e96a4d80"
    resp = client.post(PAYMENT_STATUS, json=_credit_body(op_id))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ingested"] is False and body["reason"] == "account_operation_ignored"
    assert _run(_count(async_session_factory)) == 0


def test_account_operation_hold_stage(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    resp = client.post(BASE, json=_credit_body("hold-0001", status="authorization"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ingested"] is False and body["stage"] == "authorization"
    assert _run(_count(async_session_factory)) == 0


def test_account_operation_idempotent_no_write(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    op_id = "dup-777"
    first = client.post(BASE, json=_credit_body(op_id))
    second = client.post(BASE, json=_credit_body(op_id))
    assert first.status_code == 200 and second.status_code == 200
    # Ни первый, ни повторный не пишут строку — оба просто ack.
    assert first.json()["inserted"] == 0 and second.json()["inserted"] == 0
    assert _run(_count(async_session_factory)) == 0


def test_account_operation_missing_operation_id_is_422(client: TestClient) -> None:
    # Тело-операция (есть typeOfOperation/rubleAmount), но без operationId — 422.
    resp = client.post(BASE, json={"typeOfOperation": "Credit", "rubleAmount": "100"})
    assert resp.status_code == 422


def test_account_operation_without_account_number_still_acked(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # Номер счёта операции больше не нужен (в ДДС не пишем) — тело подтверждается ack без записи.
    body = _credit_body("no-account-1")
    del body["accountNumber"]
    resp = client.post(BASE, json=body)
    assert resp.status_code == 200
    assert resp.json()["ingested"] is False
    assert _run(_count(async_session_factory)) == 0


def test_account_operation_token_enforced_when_configured(client: TestClient) -> None:
    client.app.dependency_overrides[get_settings] = lambda: Settings(tbank_webhook_token="s3cret")
    try:
        body = _credit_body("auth-check-1")
        assert client.post(BASE, json=body).status_code == 401
        bad = client.post(BASE, json=body, headers={"Authorization": "Bearer wrong"})
        assert bad.status_code == 401
        ok = client.post(BASE, json=body, headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200 and ok.json()["ingested"] is False
    finally:
        client.app.dependency_overrides.pop(get_settings, None)

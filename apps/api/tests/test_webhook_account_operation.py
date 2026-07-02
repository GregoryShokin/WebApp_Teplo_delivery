"""Вебхук «Операция по счёту» T-Банка: realtime-наполнение журнала ДДС из выписки.

Покрывает: запись проведённой операции, гейт авторизационного холда, идемпотентность
дубль-фаера (UPDATE, не вторая вставка → баланс не задваивается), 422 без operationId,
авторизацию по общему токену.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.models import BankOperation

BASE = "/api/v1/webhooks/tbank/account-operation"


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


async def _ops(factory: async_sessionmaker[AsyncSession], operation_id: str) -> list[BankOperation]:
    async with factory() as session:
        return list(
            (
                await session.scalars(
                    select(BankOperation).where(
                        BankOperation.provider == "tbank",
                        BankOperation.provider_operation_id == operation_id,
                    )
                )
            ).all()
        )


def test_account_operation_ingests_credit(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    op_id = "126cc5a2-41ca-0083-9017-5863a14692df"
    resp = client.post(BASE, json=_credit_body(op_id))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ingested"] is True and body["stage"] == "transaction"
    assert body["inserted"] == 1 and body["updated"] == 0

    rows = _run(_ops(async_session_factory, op_id))
    assert len(rows) == 1
    op = rows[0]
    assert op.direction == "in"
    assert op.amount == Decimal("50000")
    assert op.counterparty_name_raw == 'ООО "Контрагент"'
    assert op.counterparty_inn_raw == "7700000000"
    # Общий нормализатор (как у поллинга) читает назначение из description.
    assert op.payment_purpose == "Оплата по договору 22"
    assert op.raw_payload["operationStatus"] == "Transaction"


def test_payment_status_route_also_ingests_account_operation(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Банк по заявке шлёт операции по счёту на /tbank/payment-status — этот URL тоже
    вливает их в журнал ДДС (объединение: реальный трафик идёт на payment-status)."""
    op_id = "7ea7de7e-91b3-0059-a742-59a5e96a4d80"
    resp = client.post("/api/v1/webhooks/tbank/payment-status", json=_credit_body(op_id))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ingested"] is True and body["stage"] == "transaction"
    assert body["inserted"] == 1 and body["updated"] == 0
    rows = _run(_ops(async_session_factory, op_id))
    assert len(rows) == 1 and rows[0].direction == "in"


def test_account_operation_hold_not_ingested(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    op_id = "hold-0001"
    resp = client.post(BASE, json=_credit_body(op_id, status="authorization"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ingested"] is False and body["stage"] == "authorization"
    assert _run(_ops(async_session_factory, op_id)) == []


def test_account_operation_idempotent_on_refire(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    op_id = "dup-777"
    first = client.post(BASE, json=_credit_body(op_id))
    second = client.post(BASE, json=_credit_body(op_id))
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["inserted"] == 1
    # Повторная доставка той же операции — UPDATE, не вторая строка.
    assert second.json()["inserted"] == 0 and second.json()["updated"] == 1
    assert len(_run(_ops(async_session_factory, op_id))) == 1


def test_account_operation_hold_then_transaction(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    op_id = "stage-flow-1"
    hold = client.post(BASE, json=_credit_body(op_id, status="authorization"))
    assert hold.json()["ingested"] is False
    assert _run(_ops(async_session_factory, op_id)) == []
    # Тот же operationId приходит как проведённая — теперь пишем ровно одну строку.
    posted = client.post(BASE, json=_credit_body(op_id, status="transaction"))
    assert posted.json()["ingested"] is True and posted.json()["inserted"] == 1
    assert len(_run(_ops(async_session_factory, op_id))) == 1


def test_account_operation_missing_operation_id_is_422(client: TestClient) -> None:
    resp = client.post(BASE, json={"typeOfOperation": "Credit", "rubleAmount": "100"})
    assert resp.status_code == 422


def test_account_operation_missing_account_number_is_422(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # Без accountNumber операция стала бы account_id=NULL и выпала из баланса — отдаём 422,
    # а не молча сохраняем невидимую строку.
    body = _credit_body("no-account-1")
    del body["accountNumber"]
    resp = client.post(BASE, json=body)
    assert resp.status_code == 422
    assert _run(_ops(async_session_factory, "no-account-1")) == []


def test_account_operation_token_enforced_when_configured(client: TestClient) -> None:
    client.app.dependency_overrides[get_settings] = lambda: Settings(tbank_webhook_token="s3cret")
    try:
        body = _credit_body("auth-check-1")
        assert client.post(BASE, json=body).status_code == 401
        bad = client.post(BASE, json=body, headers={"Authorization": "Bearer wrong"})
        assert bad.status_code == 401
        ok = client.post(BASE, json=body, headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200 and ok.json()["ingested"] is True
    finally:
        client.app.dependency_overrides.pop(get_settings, None)

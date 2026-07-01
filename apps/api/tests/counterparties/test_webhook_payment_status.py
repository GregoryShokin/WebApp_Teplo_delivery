"""Webhook «Статус платежа» T-Банка: авторизация, сопоставление по provider_ref, гашение.

Оплату черновика фиксирует ТОЛЬКО статус платёжного документа (по ``provider_ref``). Тело с
``operationId`` (операция по счёту) на этом URL игнорируется — не гасит черновик и не пишется
в ДДС, чтобы выписка не задваивала статусную оплату.
"""

from __future__ import annotations

import asyncio
import uuid

from cp_helpers import make_counterparty, make_draft, make_invoice
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.models import CounterpartyPaymentDraft, SupplierInvoice

BASE = "/api/v1/webhooks/tbank/payment-status"


def _run(coro):
    return asyncio.run(coro)


async def _seed(factory, *, provider_ref: str = "pay-1") -> tuple[uuid.UUID, uuid.UUID]:
    async with factory() as session:
        cp = await make_counterparty(session, name="Поставщик")
        draft = await make_draft(session, counterparty_id=cp.id, amount="1000.00")
        draft.provider_ref = provider_ref
        await session.flush()
        inv = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", draft_id=draft.id
        )
        await session.commit()
        return draft.id, inv.id


def test_webhook_settles_invoice_on_executed(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, invoice_id = _run(_seed(async_session_factory))
    resp = client.post(BASE, json={"paymentId": "pay-1", "status": "executed"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["matched"] is True and body["draft_status"] == "paid"

    async def _check() -> str:
        async with async_session_factory() as session:
            inv = await session.get(SupplierInvoice, invoice_id)
            return inv.payment_status

    assert _run(_check()) == "paid"


def test_webhook_unknown_payment_acked_not_matched(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _run(_seed(async_session_factory))
    resp = client.post(BASE, json={"paymentId": "does-not-exist", "status": "executed"})
    assert resp.status_code == 200  # 200, чтобы банк не ретраил
    assert resp.json()["matched"] is False


def test_webhook_missing_payment_id_is_422(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _run(_seed(async_session_factory))
    resp = client.post(BASE, json={"status": "executed"})
    assert resp.status_code == 422


def test_webhook_is_idempotent(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _run(_seed(async_session_factory))
    first = client.post(BASE, json={"paymentId": "pay-1", "status": "executed"})
    second = client.post(BASE, json={"paymentId": "pay-1", "status": "executed"})
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["draft_status"] == "paid"


def test_webhook_deleted_status_reverts_invoice_to_unpaid(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Статус DELETED (черновик отозван в банке) → накладная возвращается в «неоплачено»
    (draft_id снят), черновик становится deleted. Деньги не двигались."""
    draft_id, invoice_id = _run(_seed(async_session_factory))
    resp = client.post(BASE, json={"paymentId": "pay-1", "status": "DELETED"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["draft_status"] == "deleted"

    async def _check() -> tuple[str, object]:
        async with async_session_factory() as session:
            inv = await session.get(SupplierInvoice, invoice_id)
            return inv.payment_status, inv.draft_id

    inv_status, inv_draft_id = _run(_check())
    assert inv_status != "paid"
    assert inv_draft_id is None  # накладная снова доступна к оплате


_PAYER_ACCOUNT = "40802810000000012345"


def _debit_body(operation_id: str, *, doc_number: str = "654321", amount: str = "1000.00") -> dict:
    """Расходная операция по счёту (формат выписки T-Банка). На статусный контур такое тело
    приходит как выписка — раньше гасило черновик по documentNumber, теперь игнорируется."""
    return {
        "operationId": operation_id,
        "typeOfOperation": "Debit",
        "accountNumber": _PAYER_ACCOUNT,
        "documentNumber": doc_number,
        "operationAmount": amount,
        "accountAmount": amount,
        "rubleAmount": amount,
        "operationStatus": "Transaction",
        "operationDate": "2026-06-25T10:00:00Z",
        "payPurpose": "Оплата поставщику по счёту",
        "description": "Оплата поставщику",
        "payer": {"account": _PAYER_ACCOUNT, "name": "ИП Шокина Е.А."},
        "receiver": {"account": "40702810900000099999", "name": "Поставщик", "inn": "7700000000"},
    }


async def _seed_op(
    factory, *, doc_number: str = "654321", amount: str = "1000.00"
) -> tuple[uuid.UUID, uuid.UUID]:
    """Открытый черновик «банк по реквизитам» с накладной; documentNumber в payload."""
    async with factory() as session:
        cp = await make_counterparty(session, name="Поставщик", inn="7700000000")
        draft = await make_draft(session, counterparty_id=cp.id, amount=amount)
        draft.provider_ref = "bank-doc-id-1"
        draft.payload = {"documentNumber": doc_number, "accountNumber": _PAYER_ACCOUNT}
        await session.flush()
        inv = await make_invoice(session, counterparty_id=cp.id, amount=amount, draft_id=draft.id)
        await session.commit()
        return draft.id, inv.id


def test_operation_like_body_ignored_not_settled(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Операция по счёту (есть operationId) на статусном URL игнорируется, НЕ гасит черновик:
    оплату фиксирует только статус платёжного документа. Регресс: раньше документ-матч гасил."""
    draft_id, invoice_id = _run(_seed_op(async_session_factory))
    resp = client.post(BASE, json=_debit_body("op-settle-1"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ingested"] is False and body["settled_draft"] is None
    assert body["reason"] == "account_operation_ignored"

    async def _check() -> tuple[str, str]:
        async with async_session_factory() as session:
            inv = await session.get(SupplierInvoice, invoice_id)
            draft = await session.get(CounterpartyPaymentDraft, draft_id)
            return inv.payment_status, draft.status

    inv_status, draft_status = _run(_check())
    # Черновик и накладная не двинулись — ждут статусный webhook платёжного документа.
    assert inv_status != "paid"
    assert draft_status in ("created", "updated")


def test_operation_like_body_without_account_still_acked(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Тело-операция без accountNumber тоже просто подтверждается (счёт больше не нужен —
    в ДДС не пишем), 200 + reason=account_operation_ignored, без 422."""
    body = _debit_body("op-nomatch-1", doc_number="999999")
    del body["accountNumber"]
    resp = client.post(BASE, json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["reason"] == "account_operation_ignored"


def test_webhook_token_enforced_when_configured(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _run(_seed(async_session_factory))
    client.app.dependency_overrides[get_settings] = lambda: Settings(tbank_webhook_token="s3cret")
    try:
        # без заголовка → 401
        assert (
            client.post(BASE, json={"paymentId": "pay-1", "status": "executed"}).status_code == 401
        )
        # неверный токен → 401
        bad = client.post(
            BASE,
            json={"paymentId": "pay-1", "status": "executed"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert bad.status_code == 401
        # верный токен → 200
        ok = client.post(
            BASE,
            json={"paymentId": "pay-1", "status": "executed"},
            headers={"Authorization": "Bearer s3cret"},
        )
        assert ok.status_code == 200 and ok.json()["matched"] is True
        # «голый» токен без префикса Bearer (так шлёт T-Банк) → тоже 200
        ok_bare = client.post(
            BASE,
            json={"paymentId": "pay-1", "status": "executed"},
            headers={"Authorization": "s3cret"},
        )
        assert ok_bare.status_code == 200 and ok_bare.json()["matched"] is True
    finally:
        client.app.dependency_overrides.pop(get_settings, None)

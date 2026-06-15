"""Webhook «Статус платежа» T-Банка: авторизация, сопоставление по provider_ref, гашение."""

from __future__ import annotations

import asyncio
import uuid

from cp_helpers import make_counterparty, make_draft, make_invoice
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.models import SupplierInvoice

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
    finally:
        client.app.dependency_overrides.pop(get_settings, None)

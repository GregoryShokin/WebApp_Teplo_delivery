"""Рассинхрон iikoServer↔Cloud: add_payment «incoming invoice not found».

Документ есть в iikoServer (накладная запушена, PROCESSED), но ещё не виден в iikoCloud, поэтому
Cloud add_payment отвечает «incoming invoice not found». На СВЕЖЕМ документе это НЕ терминал —
джоб ретраит, пока документ не доедет в Cloud (тогда оплата проходит сама). За grace-окном —
терминал + ручной кейс. Настоящие перманентные 4xx (invalid amount) — терминал сразу.

HTTP к iiko замокан (``_call_add_payment``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from cp_helpers import make_counterparty, make_draft, make_invoice
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.counterparty_iiko_payment as mod
from app.models import IikoInvoicePaymentPush, ReconciliationCase, SupplierInvoice

NOT_FOUND = (400, {"message": "incoming invoice not found"})
REAL_400 = (400, {"message": "invalid amount in JSON"})
OK_201 = (201, {"documentId": "x", "accountingTransactionId": "t-1"})


def _fake(calls: list, status: int, body: dict):
    def _f(payload: dict):
        calls.append(payload)
        return status, body

    return _f


async def _seed_paid_iiko_invoice(
    factory: async_sessionmaker[AsyncSession], *, amount: str = "1000.00"
) -> uuid.UUID:
    """Оплаченная через банк-черновик iiko-накладная — то, что берёт mirror_paid_iiko_invoices."""
    async with factory() as session:
        cp = await make_counterparty(session, name="Поставщик NF", inn="7700000123")
        draft = await make_draft(session, counterparty_id=cp.id, amount=amount, status="paid")
        inv = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount=amount,
            source="iiko",
            external_id=f"doc-{uuid.uuid4()}",
            payment_status="paid",
            draft_id=draft.id,
        )
        await session.commit()
        return inv.id


async def _set_pushed_at(
    factory: async_sessionmaker[AsyncSession], invoice_id: uuid.UUID, dt: datetime
) -> None:
    async with factory() as session:
        inv = await session.get(SupplierInvoice, invoice_id)
        assert inv is not None
        inv.iiko_pushed_at = dt
        await session.commit()


async def _push_row(
    factory: async_sessionmaker[AsyncSession], invoice_id: uuid.UUID
) -> IikoInvoicePaymentPush | None:
    async with factory() as session:
        return await session.scalar(
            select(IikoInvoicePaymentPush).where(
                IikoInvoicePaymentPush.idempotency_key == f"invoice:{invoice_id}"
            )
        )


async def _case_count(
    factory: async_sessionmaker[AsyncSession], invoice_id: uuid.UUID
) -> int:
    async with factory() as session:
        rows = (
            await session.scalars(
                select(ReconciliationCase).where(
                    ReconciliationCase.kind == "iiko_payment_unsettled",
                    ReconciliationCase.payload["invoice_id"].astext == str(invoice_id),
                )
            )
        ).all()
        return len(list(rows))


async def test_not_found_fresh_is_not_terminal_and_recovers(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    inv_id = await _seed_paid_iiko_invoice(async_session_factory)  # created_at=now → свежий

    # 1) Документ ещё не в Cloud → not found. НЕ терминал: attempts не докручен, кейс заведён.
    monkeypatch.setattr(mod, "_call_add_payment", _fake([], *NOT_FOUND))
    async with async_session_factory() as session:
        await mod.mirror_paid_iiko_invoices(session)
    row = await _push_row(async_session_factory, inv_id)
    assert row is not None
    assert row.status == "error"
    assert row.attempts < mod.MAX_PUSH_ATTEMPTS  # не заблокирован → джоб повторит
    assert await _case_count(async_session_factory, inv_id) == 1

    # 2) На следующем проходе документ доехал в Cloud → оплата проходит сама (авто-восстановление).
    monkeypatch.setattr(mod, "_call_add_payment", _fake([], *OK_201))
    async with async_session_factory() as session:
        result = await mod.mirror_paid_iiko_invoices(session)
    assert result["ok"] == 1
    row2 = await _push_row(async_session_factory, inv_id)
    assert row2 is not None and row2.status == "ok"


async def test_not_found_stale_becomes_terminal(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    inv_id = await _seed_paid_iiko_invoice(async_session_factory)
    await _set_pushed_at(
        async_session_factory, inv_id, datetime.now(UTC) - timedelta(days=10)
    )  # старше grace-окна

    monkeypatch.setattr(mod, "_call_add_payment", _fake([], *NOT_FOUND))
    async with async_session_factory() as session:
        await mod.mirror_paid_iiko_invoices(session)
    row = await _push_row(async_session_factory, inv_id)
    assert row is not None and row.attempts >= mod.MAX_PUSH_ATTEMPTS  # заблокирован (терминал)
    assert await _case_count(async_session_factory, inv_id) == 1

    # Заблокирован → следующий проход в iiko НЕ ходит.
    calls: list = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake(calls, *NOT_FOUND))
    async with async_session_factory() as session:
        await mod.mirror_paid_iiko_invoices(session)
    assert calls == []


async def test_real_permanent_4xx_counts_attempts_towards_cap(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    inv_id = await _seed_paid_iiko_invoice(async_session_factory)  # свежий

    monkeypatch.setattr(mod, "_call_add_payment", _fake([], *REAL_400))
    # Настоящий перманентный 4xx (invalid amount) — НЕ «not found»: попытка засчитывается (в отличие
    # от not-found, где attempts компенсируется), кап добирается ретраями, заводится кейс.
    async with async_session_factory() as session:
        await mod.mirror_paid_iiko_invoices(session)
    row = await _push_row(async_session_factory, inv_id)
    assert row is not None and row.attempts == 1
    async with async_session_factory() as session:
        await mod.mirror_paid_iiko_invoices(session)
    row2 = await _push_row(async_session_factory, inv_id)
    assert row2 is not None and row2.attempts == 2  # растёт к капу, не «cap сразу»
    assert await _case_count(async_session_factory, inv_id) == 1

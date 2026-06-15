"""Авто-гашение накладных по статусу банковского платежа (замена ручного мэчинга).

Статус платежа двигает черновик и привязанные накладные по provider_ref, без выписки.
"""

from __future__ import annotations

from decimal import Decimal

from cp_helpers import make_counterparty, make_draft, make_invoice
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import InvoicePaymentAllocation, SupplierInvoice
from app.services.bank_payment_status import apply_payment_status, classify_payment_status


def test_classify_payment_status() -> None:
    assert classify_payment_status("executed") == "paid"
    assert classify_payment_status("ИСПОЛНЕН") == "paid"
    assert classify_payment_status("declined") == "failed"
    assert classify_payment_status("отклонён") == "failed"
    assert classify_payment_status("processing") == "pending"
    assert classify_payment_status(None) == "pending"


async def _alloc_count(session: AsyncSession, invoice_id) -> int:
    return await session.scalar(
        select(func.count())
        .select_from(InvoicePaymentAllocation)
        .where(InvoicePaymentAllocation.invoice_id == invoice_id)
    )


async def test_paid_settles_all_draft_invoices(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик")
        draft = await make_draft(session, counterparty_id=cp.id, amount="3000.00")
        inv1 = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", draft_id=draft.id
        )
        inv2 = await make_invoice(
            session, counterparty_id=cp.id, amount="2000.00", draft_id=draft.id
        )
        await session.commit()

        status = await apply_payment_status(session, draft=draft, raw_status="executed")
        assert status == "paid"
        for inv in (inv1, inv2):
            await session.refresh(inv)
            assert inv.payment_status == "paid"
            assert await _alloc_count(session, inv.id) == 1
        # Аллокация без bank_operation_id (оплата по статусу платежа, не по выписке).
        alloc = await session.scalar(
            select(InvoicePaymentAllocation).where(InvoicePaymentAllocation.invoice_id == inv1.id)
        )
        assert alloc.source_kind == "bank" and alloc.bank_operation_id is None


async def test_paid_is_idempotent(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="P")
        draft = await make_draft(session, counterparty_id=cp.id, amount="1000.00")
        inv = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", draft_id=draft.id
        )
        await session.commit()

        await apply_payment_status(session, draft=draft, raw_status="executed")
        await apply_payment_status(session, draft=draft, raw_status="executed")
        assert await _alloc_count(session, inv.id) == 1  # no double allocation


async def test_paid_tops_up_partial_payment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="P")
        draft = await make_draft(session, counterparty_id=cp.id, amount="1000.00")
        inv = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="1000.00",
            payment_status="partially_paid",
            draft_id=draft.id,
        )
        session.add(
            InvoicePaymentAllocation(
                invoice_id=inv.id, source_kind="cash", amount=Decimal("400.00")
            )
        )
        await session.commit()

        await apply_payment_status(session, draft=draft, raw_status="executed")
        await session.refresh(inv)
        assert inv.payment_status == "paid"
        total = await session.scalar(
            select(func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0)).where(
                InvoicePaymentAllocation.invoice_id == inv.id
            )
        )
        assert total == Decimal("1000.00")  # 400 cash + 600 bank top-up


async def test_failed_unlinks_invoices(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="P")
        draft = await make_draft(session, counterparty_id=cp.id, amount="1000.00")
        inv = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", draft_id=draft.id
        )
        await session.commit()
        inv_id = inv.id

        status = await apply_payment_status(session, draft=draft, raw_status="declined")
        assert status == "failed"
        refreshed = await session.get(SupplierInvoice, inv_id)
        assert refreshed.draft_id is None  # вернулась в «неоплачено», можно отправить заново
        assert refreshed.payment_status == "unpaid"
        assert await _alloc_count(session, inv_id) == 0


async def test_paid_does_not_revive_failed_draft(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Out-of-order «исполнен» после «отклонён» НЕ воскрешает failed-черновик: накладные уже
    отвязаны, и draft не должен молча стать paid с нулём аллокаций."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="P")
        draft = await make_draft(session, counterparty_id=cp.id, amount="1000.00")
        inv = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", draft_id=draft.id
        )
        await session.commit()
        inv_id = inv.id

        await apply_payment_status(session, draft=draft, raw_status="declined")
        status = await apply_payment_status(session, draft=draft, raw_status="executed")
        assert status == "failed"  # не воскрешён
        refreshed = await session.get(SupplierInvoice, inv_id)
        assert refreshed.payment_status == "unpaid" and refreshed.draft_id is None
        assert await _alloc_count(session, inv_id) == 0


async def test_pending_is_noop(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="P")
        draft = await make_draft(session, counterparty_id=cp.id, amount="1000.00")
        inv = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", draft_id=draft.id
        )
        await session.commit()

        status = await apply_payment_status(session, draft=draft, raw_status="processing")
        assert status == "created"
        await session.refresh(inv)
        assert inv.payment_status == "unpaid"
        assert inv.draft_id == draft.id

"""АУДИТ-5 (проверка работы email-агента): усушка суммы уже ЗАЧТЁННОГО закрывающего.

Дверь пере-разбора (confirm_intake_with_review) синхронизирует amount с распознанным. Рост
суммы агент покрыл (дозачёт через apply_closing_document). УМЕНЬШЕНИЕ — нет: закрывающий,
съевший аванс 1000, после правки суммы на 400 продолжает держать 1000 зачёта — аванс
пересписан на 600, ДЗ занижена молча.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from cp_helpers import make_counterparty, make_invoice, make_wallet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CashflowTransaction,
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services.supplier_prepayments import (
    apply_closing_document,
    ensure_prepayment_from_bank_transaction,
)


async def _receivable(session: AsyncSession, counterparty_id: uuid.UUID) -> Decimal:
    rows = (
        await session.scalars(
            select(SupplierPrepayment).where(
                SupplierPrepayment.counterparty_id == counterparty_id,
                SupplierPrepayment.status.in_(("open", "partially_settled")),
            )
        )
    ).all()
    return sum(
        (Decimal(str(r.amount)) - Decimal(str(r.amount_settled)) for r in rows), Decimal("0.00")
    )


async def test_closing_amount_shrink_returns_prepayment_excess(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Аванс 1000 → УПД 1000 (зачёт полный) → пере-разбор исправил сумму УПД на 400.
    Избыток зачёта 600 обязан вернуться предоплате: ДЗ 600, аллокаций на УПД ровно 400."""
    from app.services.email_invoice_ingest import _resync_closing_after_edit

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Усушка-УПД", inn="6155994001")
        wallet = await make_wallet(session, name="Банк-усушка", wallet_type="bank")
        tx = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal("1000.00"),
            operation_date=date(2026, 7, 10),
            counterparty_id=cp.id,
            source_kind="bank_operation",
            payment_purpose="аванс",
            quality_status="auto",
        )
        session.add(tx)
        await session.flush()
        pre = await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()
        assert pre is not None and pre.amount == Decimal("1000.00")

        upd = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="1000.00",
            doc_kind="closing",
            number="УПД-усушка",
            invoice_date=date(2026, 7, 12),
            operational_scope="finance",
        )
        await apply_closing_document(session, upd, as_of=date(2026, 7, 13))
        await session.commit()
        assert await _receivable(session, cp.id) == Decimal("0.00")
        assert (await session.get(SupplierInvoice, upd.id)).payment_status == "paid"

        # Оператор пере-разобрал письмо: правильная сумма УПД — 400 (опечатка распознавания).
        invoice = await session.get(SupplierInvoice, upd.id)
        invoice.amount = Decimal("400.00")
        await session.flush()
        await _resync_closing_after_edit(session, invoice)
        await session.commit()

        allocated = await session.scalar(
            select(func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0)).where(
                InvoicePaymentAllocation.invoice_id == upd.id
            )
        )
        dz = await _receivable(session, cp.id)
        assert (Decimal(str(allocated)), dz) == (Decimal("400.00"), Decimal("600.00")), (
            f"Аллокаций {allocated} на УПД суммой 400, ДЗ {dz} — избыток зачёта не вернулся "
            "предоплате после усушки суммы"
        )

"""Match bank operations to counterparty payment drafts and allocate to invoices.

A draft sent to the bank is not a payment; the real cash fact arrives later in the
bank feed (``bank_operations``) and in the cash journal (``cashflow_transactions``).
This module ties those facts to invoices via ``invoice_payment_allocation``, marking
invoices ``partially_paid`` / ``paid`` once allocations cover their amount. Cash facts
are never created here — they stay sourced from the bank / cash journal.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import (
    BankOperation,
    Counterparty,
    CounterpartyPaymentDraft,
    InvoicePaymentAllocation,
    SupplierInvoice,
)

ACTIVE_DRAFT_STATUSES = ("created", "updated")


class CounterpartyMatchError(RuntimeError):
    """Domain error for matching/allocation (maps to HTTP 409)."""


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def _allocated_amount(session: AsyncSession, invoice_id: uuid.UUID) -> Decimal:
    total = await session.scalar(
        select(func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0)).where(
            InvoicePaymentAllocation.invoice_id == invoice_id
        )
    )
    return _money(total)


async def _invoice_remaining(session: AsyncSession, invoice: SupplierInvoice) -> Decimal:
    return _money(invoice.amount) - await _allocated_amount(session, invoice.id)


async def _recompute_status(session: AsyncSession, invoice: SupplierInvoice) -> None:
    if invoice.payment_status == "void":
        return
    allocated = await _allocated_amount(session, invoice.id)
    amount = _money(invoice.amount)
    if allocated <= 0:
        invoice.payment_status = "unpaid"
    elif allocated < amount:
        invoice.payment_status = "partially_paid"
    else:
        invoice.payment_status = "paid"


async def _op_already_allocated(session: AsyncSession, bank_operation_id: uuid.UUID) -> bool:
    found = await session.scalar(
        select(InvoicePaymentAllocation.id)
        .where(InvoicePaymentAllocation.bank_operation_id == bank_operation_id)
        .limit(1)
    )
    return found is not None


async def _draft_invoices(
    session: AsyncSession, draft_id: uuid.UUID
) -> list[SupplierInvoice]:
    return list(
        (
            await session.execute(
                select(SupplierInvoice)
                .where(SupplierInvoice.draft_id == draft_id)
                .order_by(SupplierInvoice.invoice_date.nulls_last(), SupplierInvoice.created_at)
            )
        )
        .scalars()
        .all()
    )


async def _candidate_drafts(
    session: AsyncSession,
    *,
    inn: str,
    op_date: date,
    window_days: int,
) -> list[CounterpartyPaymentDraft]:
    rows = list(
        (
            await session.execute(
                select(CounterpartyPaymentDraft)
                .join(Counterparty, Counterparty.id == CounterpartyPaymentDraft.counterparty_id)
                .where(
                    Counterparty.inn == inn,
                    CounterpartyPaymentDraft.status.in_(ACTIVE_DRAFT_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    candidates: list[CounterpartyPaymentDraft] = []
    for draft in rows:
        draft_date = draft.created_at.date()
        if draft_date <= op_date <= draft_date + timedelta(days=window_days):
            candidates.append(draft)
    return candidates


async def allocate_bank_operation_to_draft(
    session: AsyncSession,
    *,
    bank_operation_id: uuid.UUID,
    draft_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
    commit: bool = True,
) -> CounterpartyPaymentDraft:
    """Allocate a bank operation across a draft's invoices FIFO and update statuses."""
    operation = await session.get(BankOperation, bank_operation_id)
    if operation is None:
        raise CounterpartyMatchError("Банковская операция не найдена")
    draft = await session.get(CounterpartyPaymentDraft, draft_id)
    if draft is None:
        raise CounterpartyMatchError("Черновик не найден")

    invoices = await _draft_invoices(session, draft_id)
    if not invoices:
        raise CounterpartyMatchError("К черновику не привязаны накладные")

    pool = _money(abs(operation.amount))
    for invoice in invoices:
        if pool <= 0:
            break
        remaining = await _invoice_remaining(session, invoice)
        if remaining <= 0:
            continue
        allocation_amount = min(remaining, pool)
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id,
                source_kind="bank",
                bank_operation_id=operation.id,
                amount=allocation_amount,
                created_by_user_id=actor_user_id,
            )
        )
        pool -= allocation_amount
        await session.flush()
        await _recompute_status(session, invoice)

    if all(invoice.payment_status == "paid" for invoice in invoices):
        draft.status = "paid"
        draft.synced_at = datetime.now(tz=UTC)

    if commit:
        await session.commit()
        await session.refresh(draft)
    return draft


async def auto_match_bank_operations(
    session: AsyncSession,
    *,
    window_days: int | None = None,
    operation_ids: Sequence[uuid.UUID] | None = None,
) -> dict[str, list[Any]]:
    """Auto-match outgoing bank ops to drafts by exact INN + amount within a window.

    Exactly one matching draft → allocated automatically. Same-INN drafts with a
    different amount (or several candidates) are returned as ``needs_review`` for a
    manager to confirm.
    """
    window = window_days if window_days is not None else get_settings().counterparty_match_window_days
    query = select(BankOperation).where(BankOperation.direction == "out")
    if operation_ids is not None:
        query = query.where(BankOperation.id.in_(list(operation_ids)))
    operations = list((await session.execute(query)).scalars().all())

    matched: list[dict[str, Any]] = []
    needs_review: list[dict[str, Any]] = []
    for operation in operations:
        if not operation.counterparty_inn_raw:
            continue
        if await _op_already_allocated(session, operation.id):
            continue
        candidates = await _candidate_drafts(
            session,
            inn=operation.counterparty_inn_raw,
            op_date=operation.operation_date,
            window_days=window,
        )
        if not candidates:
            continue
        op_amount = _money(abs(operation.amount))
        exact = [draft for draft in candidates if _money(draft.amount) == op_amount]
        if len(exact) == 1:
            await allocate_bank_operation_to_draft(
                session,
                bank_operation_id=operation.id,
                draft_id=exact[0].id,
                actor_user_id=None,
                commit=False,
            )
            matched.append({"bank_operation_id": operation.id, "draft_id": exact[0].id})
        else:
            needs_review.append(
                {
                    "bank_operation_id": operation.id,
                    "candidate_draft_ids": [draft.id for draft in candidates],
                }
            )
    await session.commit()
    return {"matched": matched, "needs_review": needs_review}


async def find_match_candidates(
    session: AsyncSession, bank_operation_id: uuid.UUID, *, window_days: int | None = None
) -> list[CounterpartyPaymentDraft]:
    window = window_days if window_days is not None else get_settings().counterparty_match_window_days
    operation = await session.get(BankOperation, bank_operation_id)
    if operation is None or not operation.counterparty_inn_raw:
        return []
    return await _candidate_drafts(
        session,
        inn=operation.counterparty_inn_raw,
        op_date=operation.operation_date,
        window_days=window,
    )


async def allocate_cash_to_invoice(
    session: AsyncSession,
    *,
    invoice_id: uuid.UUID,
    amount: Decimal,
    cashflow_transaction_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> SupplierInvoice:
    """Manually allocate a cash payment (nal) to one invoice — the split counterpart."""
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise CounterpartyMatchError("Накладная не найдена")
    if invoice.payment_status == "void":
        raise CounterpartyMatchError("Накладная аннулирована")
    requested = _money(amount)
    remaining = await _invoice_remaining(session, invoice)
    if requested <= 0 or requested > remaining:
        raise CounterpartyMatchError("Сумма аллокации вне допустимого остатка")
    session.add(
        InvoicePaymentAllocation(
            invoice_id=invoice.id,
            source_kind="cash",
            cashflow_transaction_id=cashflow_transaction_id,
            amount=requested,
            created_by_user_id=actor_user_id,
        )
    )
    await session.flush()
    await _recompute_status(session, invoice)
    await session.commit()
    await session.refresh(invoice)
    return invoice

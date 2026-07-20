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

from sqlalchemy import func, or_, select
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


async def payment_allocated_amount(
    session: AsyncSession,
    *,
    transaction_id: uuid.UUID | None = None,
    bank_operation_id: uuid.UUID | None = None,
) -> Decimal:
    """«Бюджет платежа»: сколько денег ОДНОГО платёжного факта уже пристроено на документы.

    Один платёж живёт под ДВУМЯ ключами — проводка ДДС (``cashflow_transaction_id``) и
    банковская операция (``bank_operation_id``), — а CHECK ``ck_invoice_allocation_single_source``
    запрещает аллокации нести оба сразу. Поэтому каждый счётчик по отдельности видит лишь свою
    половину платежа: правило 1 метит зачёты кредиторки ПРОВОДКОЙ, банковская сверка метит оплату
    счёта ОПЕРАЦИЕЙ, и друг друга они не видят. Отсюда росли задвоение дебиторки (одни деньги
    дважды становились авансом) и перерасход платежа (1000 ₽ закрывали документов на 1300 ₽).

    Здесь мост между ключами (``BankOperation.cashflow_transaction_id``) сшивается и сумма
    считается по ОБОИМ — единственный честный ответ на вопрос «сколько из этого платежа уже
    израсходовано». Аллокации ``source_kind='prepayment'`` не в счёт: их финансирует ранее
    выданная предоплата, а не сам платёж.
    """
    tx_ids: set[uuid.UUID] = set()
    op_ids: set[uuid.UUID] = set()
    if transaction_id is not None:
        tx_ids.add(transaction_id)
        op_ids.update(
            (
                await session.scalars(
                    select(BankOperation.id).where(
                        BankOperation.cashflow_transaction_id == transaction_id
                    )
                )
            ).all()
        )
    if bank_operation_id is not None:
        op_ids.add(bank_operation_id)
        operation = await session.get(BankOperation, bank_operation_id)
        if operation is not None and operation.cashflow_transaction_id is not None:
            tx_ids.add(operation.cashflow_transaction_id)
    if not tx_ids and not op_ids:
        return _money(0)

    key_filters = []
    if tx_ids:
        key_filters.append(InvoicePaymentAllocation.cashflow_transaction_id.in_(tx_ids))
    if op_ids:
        key_filters.append(InvoicePaymentAllocation.bank_operation_id.in_(op_ids))
    total = await session.scalar(
        select(func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0)).where(
            InvoicePaymentAllocation.source_kind != "prepayment",
            or_(*key_filters),
        )
    )
    return _money(total)


async def _invoice_remaining(session: AsyncSession, invoice: SupplierInvoice) -> Decimal:
    """Непогашенный остаток документа.

    У бартерного ЗАЙМА долг гасится не только деньгами: товарные возвраты живут в леджере
    ``BarterReturnLine``, прощённый недовес — в ``barter_writeoff_amount``. По аллокациям такой
    остаток завышен, поэтому поправка стоит ЗДЕСЬ, в единой точке: эту функцию зовут все двери
    оплаты (черновик в банк, paid-переход после исполнения платежа, FIFO по черновику, сверка,
    оплата с кошелька, выдача с Сейфа, классификатор). Точечные поправки в отдельных дверях
    уже приводили к тому, что одна из них платила за уже возвращённый товар второй раз.
    """
    if invoice.barter_role == "loan":
        from app.services.warehouse_invoices import loan_settled_value

        return _money(invoice.amount) - await loan_settled_value(session, invoice)
    return _money(invoice.amount) - await _allocated_amount(session, invoice.id)


async def _recompute_status(session: AsyncSession, invoice: SupplierInvoice) -> None:
    if invoice.payment_status == "void":
        # Статус аннулированного не трогаем, но ЧОКПОИНТ ниже обязан отработать: ДЗ по счёту
        # следует за деньгами, а не за статусом. Деньги уходили и остались (аллокации живы) —
        # ДЗ живёт (деньги у поставщика без закрывающего); оплату сняли — чокпоинт приберёт ДЗ.
        # Ранний return ДО чокпоинта замораживал ДЗ аннулированного счёта навсегда.
        if invoice.doc_kind == "bill":
            from app.services.supplier_prepayments import reconcile_bill_prepayment

            await reconcile_bill_prepayment(session, invoice)
        return
    allocated = await _allocated_amount(session, invoice.id)
    amount = _money(invoice.amount)
    if allocated <= 0:
        invoice.payment_status = "unpaid"
    elif allocated < amount:
        invoice.payment_status = "partially_paid"
    else:
        invoice.payment_status = "paid"
    # Единый чокпоинт канона ДЗ/КЗ: оплата счёта (doc_kind='bill') — не долг, а предоплата (ДЗ).
    # Здесь сходятся ВСЕ двери гашения накладной, поэтому дебиторку по счёту заводит/синхронизирует
    # одно место (reconcile_bill_prepayment), а не каждая дверь. Ленивый импорт —
    # supplier_prepayments импортирует этот модуль (цикл). Для закрывающих чокпоинт — ранний no-op.
    if invoice.doc_kind == "bill":
        from app.services.supplier_prepayments import reconcile_bill_prepayment

        await reconcile_bill_prepayment(session, invoice)
    # Бартерный заём: статус гашения складывается из возвратов товаром (леджер BarterReturnLine)
    # И денежных оплат (аллокации) — пересчёт по одним аллокациям рассинхронизировал бы их
    # (снятие аллокации при исключении операции оставило бы barter_return_status='returned').
    # Ленивый импорт — warehouse_invoices импортирует этот модуль (цикл).
    if invoice.barter_role == "loan":
        from app.services.warehouse_invoices import sync_barter_loan_status

        await sync_barter_loan_status(session, invoice)


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

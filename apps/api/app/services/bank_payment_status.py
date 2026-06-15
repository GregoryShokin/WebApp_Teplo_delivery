"""Авто-гашение накладных по статусу банковского платежа (T-Банк).

Заменяет ручной мэчинг выписки: статус платежа (из polling ``GET /payment/{id}`` или
webhook «Статус платежа») двигает черновик ``CounterpartyPaymentDraft`` и привязанные к
нему накладные. Сопоставление — по ``provider_ref`` (идентификатор платежа у банка), без
сумм/реквизитов.

Выписка (``BankOperation``) здесь НЕ нужна: при статусе «исполнен» аллокация создаётся без
``bank_operation_id`` (CHECK ``ck_invoice_allocation_single_source`` допускает оба NULL) —
это «оплата подтверждена статусом платежа». Денежное движение ДДС формируется отдельно при
классификации выписки; двойного учёта нет, так как старый auto-match по сумме снят.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CounterpartyPaymentDraft, InvoicePaymentAllocation
from app.services.counterparty_matching import (
    _draft_invoices,
    _invoice_remaining,
    _recompute_status,
)

# Нормализованные строки статусов банка → исход. Точные значения T-Банка уточняются при
# подключении (банк присылает схему); неизвестный статус оставляет платёж «в банке».
PAID_STATUSES = frozenset(
    {
        "executed",
        "paid",
        "completed",
        "complete",
        "success",
        "successful",
        "done",
        "исполнен",
        "исполнено",
        "оплачен",
        "оплачено",
        "проведен",
        "проведён",
        "проведено",
    }
)
FAILED_STATUSES = frozenset(
    {
        "declined",
        "rejected",
        "canceled",
        "cancelled",
        "failed",
        "error",
        "отклонен",
        "отклонён",
        "отклонено",
        "отменен",
        "отменён",
        "отменено",
        "ошибка",
    }
)


def classify_payment_status(raw_status: str | None) -> str:
    """Свести строку статуса банка к ``paid`` / ``failed`` / ``pending``."""
    value = (raw_status or "").strip().lower()
    if value in PAID_STATUSES:
        return "paid"
    if value in FAILED_STATUSES:
        return "failed"
    return "pending"


async def apply_payment_status(
    session: AsyncSession,
    *,
    draft: CounterpartyPaymentDraft,
    raw_status: str | None,
    actor_user_id: uuid.UUID | None = None,
    commit: bool = True,
) -> str:
    """Продвинуть черновик и его накладные по статусу платежа. Идемпотентно: повторный
    «исполнен» по оплаченному черновику — no-op. Возвращает итоговый статус черновика."""
    outcome = classify_payment_status(raw_status)

    # Сериализуем обработку этого черновика блокировкой строки: закрывает гонку webhook↔polling
    # и дубль-доставку webhook (иначе обе транзакции прошли бы in-memory guard и создали по
    # аллокации → двойное гашение). Вторая транзакция дождётся коммита первой и увидит paid/failed.
    draft = await session.get(CounterpartyPaymentDraft, draft.id, with_for_update=True)
    if draft is None:
        return "created"

    # Переход разрешён только из активных статусов — это и идемпотентность (paid→paid no-op), и
    # защита от реанимации: out-of-order «исполнен» после «отклонён» не воскрешает failed-черновик
    # с уже отвязанными накладными.
    if outcome == "paid" and draft.status in ("created", "updated"):
        # Гасим остаток каждой накладной аллокацией без bank_operation_id (оплата по статусу
        # платежа; выписка придёт в ДДС отдельно). Уважает частичную оплату из прямого контура.
        for invoice in await _draft_invoices(session, draft.id):
            remaining = await _invoice_remaining(session, invoice)
            if remaining <= 0:
                continue
            session.add(
                InvoicePaymentAllocation(
                    invoice_id=invoice.id,
                    source_kind="bank",
                    bank_operation_id=None,
                    amount=remaining,
                    created_by_user_id=actor_user_id,
                )
            )
            await session.flush()
            await _recompute_status(session, invoice)
        draft.status = "paid"
        draft.synced_at = datetime.now(UTC)

    elif outcome == "failed" and draft.status in ("created", "updated"):
        # Платёж отклонён банком: возвращаем накладные в «неоплачено» (снимаем draft_id), чтобы
        # их можно было отправить заново.
        for invoice in await _draft_invoices(session, draft.id):
            invoice.draft_id = None
        draft.status = "failed"
        draft.last_error = f"Платёж отклонён банком: {raw_status}"[:500]
        draft.synced_at = datetime.now(UTC)

    if commit:
        await session.commit()
    return draft.status

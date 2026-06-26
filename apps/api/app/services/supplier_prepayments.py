"""Предоплаты поставщикам (дебиторка): мы платим вперёд — поставщик должен привезти.

Создание предоплаты = реальный расход денег (out-CashflowTransaction, source_kind=
'supplier_prepayment'), обычно с кошелька «Сейф». Гашение приходящих payable-накладных —
через InvoicePaymentAllocation(source_kind='prepayment'), которая денег НЕ двигает (они
ушли при создании предоплаты). Отдельный учёт от кредиторки и товарного бартера.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CashflowTransaction,
    Counterparty,
    DdsArticle,
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierPrepayment,
    Wallet,
)
from app.services.counterparty_matching import _invoice_remaining, _recompute_status
from app.services.counterparty_payments import CounterpartyPaymentError

PREPAYMENT_ARTICLE_CODE = "advance_to_supplier"
OPEN_PREPAYMENT_STATUSES = ("open", "partially_settled")


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def create_supplier_prepayment(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    wallet_id: uuid.UUID,
    amount: Decimal,
    operation_date: date,
    article_id: uuid.UUID | None = None,
    kind: str = "goods",
    note: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> SupplierPrepayment:
    """Завести предоплату поставщику: реальный расход с кошелька + запись дебиторки.

    Деньги уходят сразу (out-CashflowTransaction), возникает остаток «поставщик нам
    должен». Накладные гасятся против него позже через settle_invoice_from_prepayment.
    """
    cp = await session.get(Counterparty, counterparty_id)
    if cp is None:
        raise CounterpartyPaymentError("Контрагент не найден")

    amt = _money(amount)
    if amt <= 0:
        raise CounterpartyPaymentError("Сумма предоплаты должна быть больше нуля")

    wallet = await session.get(Wallet, wallet_id)
    if wallet is None or wallet.status != "active":
        raise CounterpartyPaymentError("Счёт не найден или неактивен")

    resolved_article_id = article_id
    if resolved_article_id is None:
        resolved_article_id = await session.scalar(
            select(DdsArticle.id).where(DdsArticle.code == PREPAYMENT_ARTICLE_CODE)
        )
    elif await session.get(DdsArticle, resolved_article_id) is None:
        raise CounterpartyPaymentError("Статья ДДС не найдена")

    transaction = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=amt,
        operation_date=operation_date,
        article_id=resolved_article_id,
        counterparty_id=counterparty_id,
        source_kind="supplier_prepayment",
        payment_purpose=f"Предоплата поставщику {cp.name}",
        comment=note,
        quality_status="final",
    )
    session.add(transaction)
    await session.flush()

    prepayment = SupplierPrepayment(
        counterparty_id=counterparty_id,
        kind=kind,
        wallet_id=wallet.id,
        amount=amt,
        amount_settled=Decimal("0.00"),
        status="open",
        cashflow_transaction_id=transaction.id,
        article_id=resolved_article_id,
        note=note,
        created_by_user_id=actor_user_id,
    )
    session.add(prepayment)
    await session.flush()
    transaction.source_id = prepayment.id  # обратная ссылка денежный факт → предоплата
    await session.commit()
    await session.refresh(prepayment)
    return prepayment


async def prepayment_remaining(prepayment: SupplierPrepayment) -> Decimal:
    return _money(prepayment.amount) - _money(prepayment.amount_settled)


async def settle_invoice_from_prepayment(
    session: AsyncSession,
    *,
    invoice_id: uuid.UUID,
    prepayment_id: uuid.UUID,
    amount: Decimal | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> SupplierInvoice:
    """Погасить (часть) накладной против остатка ранее выданной предоплаты. Денег не двигает."""
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise CounterpartyPaymentError("Накладная не найдена")
    if invoice.payment_status == "void":
        raise CounterpartyPaymentError("Накладная аннулирована")
    if invoice.direction != "payable":
        raise CounterpartyPaymentError("Доходную накладную нельзя гасить из предоплаты")
    if invoice.barter_role is not None:
        raise CounterpartyPaymentError("Бартерную накладную нельзя гасить из предоплаты")

    prepayment = await session.get(SupplierPrepayment, prepayment_id)
    if prepayment is None:
        raise CounterpartyPaymentError("Предоплата не найдена")
    if prepayment.counterparty_id != invoice.counterparty_id:
        raise CounterpartyPaymentError("Предоплата и накладная относятся к разным контрагентам")
    if prepayment.status not in OPEN_PREPAYMENT_STATUSES:
        # Возвращённую/закрытую предоплату (например 'refunded' со стейл amount_settled<amount)
        # гасить нельзя — иначе списали бы остаток уже не существующей дебиторки.
        raise CounterpartyPaymentError("Предоплата недоступна для гашения (возвращена или закрыта)")

    inv_remaining = await _invoice_remaining(session, invoice)
    pre_remaining = _money(prepayment.amount) - _money(prepayment.amount_settled)
    if inv_remaining <= 0:
        raise CounterpartyPaymentError("Накладная уже оплачена")
    if pre_remaining <= 0:
        raise CounterpartyPaymentError("Предоплата исчерпана")

    alloc = _money(amount) if amount is not None else min(inv_remaining, pre_remaining)
    alloc = min(alloc, inv_remaining, pre_remaining)
    if alloc <= 0:
        raise CounterpartyPaymentError("Сумма гашения вне допустимого остатка")

    session.add(
        InvoicePaymentAllocation(
            invoice_id=invoice.id,
            source_kind="prepayment",
            prepayment_id=prepayment.id,
            amount=alloc,
            created_by_user_id=actor_user_id,
        )
    )
    prepayment.amount_settled = _money(prepayment.amount_settled) + alloc
    prepayment.status = (
        "settled" if prepayment.amount_settled >= _money(prepayment.amount) else "partially_settled"
    )
    await session.flush()
    await _recompute_status(session, invoice)
    await session.commit()
    await session.refresh(invoice)
    return invoice


async def counterparty_prepayment_balance(
    session: AsyncSession, counterparty_id: uuid.UUID
) -> Decimal:
    """Остаток выданных предоплат контрагенту (= «поставщик нам должен»)."""
    total = await session.scalar(
        select(
            func.coalesce(
                func.sum(SupplierPrepayment.amount - SupplierPrepayment.amount_settled), 0
            )
        )
        .where(SupplierPrepayment.counterparty_id == counterparty_id)
        .where(SupplierPrepayment.status.in_(OPEN_PREPAYMENT_STATUSES))
    )
    return _money(total)

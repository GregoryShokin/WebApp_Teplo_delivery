"""Резервы (аллокации) подотчётного счёта «Сейф» — фаза 2.

Резерв (``SafeAllocation``) — намерение потратить часть остатка Сейфа: деньги ещё
на карте, поэтому резерв НЕ создаёт cashflow и НЕ двигает баланс, лишь уменьшает
«свободно» = баланс − Σ(непогашенных резервов). Расход признаётся при оплате:
каждая оплата (в т.ч. частичная) — отдельная out-``CashflowTransaction`` на Сейфе
(``source_kind='safe_payout'``, ``source_id`` = резерв), а ``amount_paid`` копит их
сумму. Инвариант: баланс = свободно + Σ(резерв.amount − резерв.amount_paid).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CashflowTransaction, SafeAllocation

SAFE_PAYOUT_SOURCE_KIND = "safe_payout"
ACTIVE_RESERVE_STATUSES = ("reserved", "partially_paid")


async def safe_reserved_total(session: AsyncSession, wallet_id: UUID) -> Decimal:
    """Σ непогашенных остатков активных резервов кошелька (для «зарезервировано»)."""
    total = await session.scalar(
        select(func.coalesce(func.sum(SafeAllocation.amount - SafeAllocation.amount_paid), 0)).where(
            SafeAllocation.wallet_id == wallet_id,
            SafeAllocation.status.in_(ACTIVE_RESERVE_STATUSES),
        )
    )
    return Decimal(total or 0)


async def create_allocation(
    session: AsyncSession,
    *,
    wallet_id: UUID,
    amount: Decimal,
    free_amount: Decimal,
    article_id: UUID | None = None,
    counterparty_id: UUID | None = None,
    purpose: str | None = None,
    created_by_user_id: UUID | None = None,
) -> SafeAllocation:
    """Создать резерв. Запрет перерезервирования: ``amount`` ≤ свободно (``free_amount``)."""
    if amount <= 0:
        raise ValueError("Сумма резерва должна быть больше нуля")
    if amount > free_amount:
        raise ValueError(
            f"Недостаточно свободных средств на Сейфе: свободно {free_amount}, запрошено {amount}"
        )
    allocation = SafeAllocation(
        wallet_id=wallet_id,
        amount=amount,
        amount_paid=Decimal("0"),
        article_id=article_id,
        counterparty_id=counterparty_id,
        purpose=purpose,
        status="reserved",
        created_by_user_id=created_by_user_id,
    )
    session.add(allocation)
    await session.flush()
    return allocation


async def pay_allocation(
    session: AsyncSession,
    allocation: SafeAllocation,
    *,
    amount: Decimal,
    operation_date: date,
) -> UUID:
    """Провести оплату резерва (полную или частичную): out-проводка с Сейфа + обновить статус."""
    if allocation.status == "cancelled":
        raise ValueError("Резерв отменён — оплата невозможна")
    if allocation.status == "paid":
        raise ValueError("Резерв уже полностью оплачен")
    if amount <= 0:
        raise ValueError("Сумма оплаты должна быть больше нуля")
    outstanding = Decimal(allocation.amount) - Decimal(allocation.amount_paid)
    if amount > outstanding:
        raise ValueError(f"Сумма оплаты ({amount}) больше остатка резерва ({outstanding})")

    leg = CashflowTransaction(
        wallet_id=allocation.wallet_id,
        direction="out",
        amount=amount,
        operation_date=operation_date,
        article_id=allocation.article_id,
        counterparty_id=allocation.counterparty_id,
        source_kind=SAFE_PAYOUT_SOURCE_KIND,
        source_id=allocation.id,
        payment_purpose=allocation.purpose,
        quality_status="final",
    )
    session.add(leg)
    allocation.amount_paid = Decimal(allocation.amount_paid) + amount
    allocation.status = (
        "paid" if allocation.amount_paid >= Decimal(allocation.amount) else "partially_paid"
    )
    await session.flush()
    return leg.id


async def cancel_allocation(session: AsyncSession, allocation: SafeAllocation) -> None:
    """Отменить резерв. Оплаченные ноги остаются, неоплаченный остаток освобождается."""
    if allocation.status == "paid":
        raise ValueError("Нельзя отменить полностью оплаченный резерв")
    if allocation.status == "cancelled":
        return
    allocation.status = "cancelled"
    await session.flush()

"""Доменная коррекция ошибочно выбранного кошелька выплаты зарплаты."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CashflowTransaction,
    DdsArticle,
    PayrollRun,
    PayrollRunEvent,
    Wallet,
)
from app.services.payroll_payouts import PAYROLL_PAYOUT_SOURCE_KIND
from app.services.payroll_reserves import (
    KASSA_WALLET_CODE,
    ensure_run_kassa_reserve,
    reconcile_run_reserves,
    run_payment_settlement,
)
from app.services.payroll_runner import PayrollConflictError, PayrollNotFoundError, money_text
from app.services.wallets import SAFE_WALLET_CODE, CashWalletError, resolve_cash_wallet

ALLOWED_CORRECTION_WALLETS = frozenset({SAFE_WALLET_CODE, KASSA_WALLET_CODE})


@dataclass(frozen=True, slots=True)
class PayrollPayoutCashflow:
    id: uuid.UUID
    wallet_id: uuid.UUID
    wallet_code: str
    wallet_name: str
    amount: Decimal
    operation_date: date
    quality_status: str
    article_code: str | None
    article_name: str | None
    purpose: str | None


@dataclass(frozen=True, slots=True)
class PayrollPayoutWalletCorrection:
    run_id: uuid.UUID
    transaction_ids: tuple[uuid.UUID, ...]
    source_wallet_id: uuid.UUID
    source_wallet_name: str
    target_wallet_id: uuid.UUID
    target_wallet_code: str
    target_wallet_name: str
    total_amount: Decimal


async def list_payroll_payout_cashflows(
    session: AsyncSession, run_id: uuid.UUID
) -> list[PayrollPayoutCashflow]:
    run = await session.get(PayrollRun, run_id)
    if run is None:
        raise PayrollNotFoundError("Ведомость не найдена")
    rows = (
        await session.execute(
            select(CashflowTransaction, Wallet, DdsArticle)
            .join(Wallet, Wallet.id == CashflowTransaction.wallet_id)
            .outerjoin(DdsArticle, DdsArticle.id == CashflowTransaction.article_id)
            .where(
                CashflowTransaction.source_kind == PAYROLL_PAYOUT_SOURCE_KIND,
                CashflowTransaction.source_id == run_id,
                CashflowTransaction.direction == "out",
            )
            .order_by(
                CashflowTransaction.operation_date,
                CashflowTransaction.created_at,
                CashflowTransaction.id,
            )
        )
    ).all()
    return [
        PayrollPayoutCashflow(
            id=txn.id,
            wallet_id=wallet.id,
            wallet_code=wallet.code,
            wallet_name=wallet.name,
            amount=Decimal(txn.amount),
            operation_date=txn.operation_date,
            quality_status=txn.quality_status,
            article_code=article.code if article is not None else None,
            article_name=article.name if article is not None else None,
            purpose=txn.payment_purpose,
        )
        for txn, wallet, article in rows
    ]


async def correct_payroll_payout_wallet(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    transaction_ids: list[uuid.UUID],
    target_wallet_code: str,
    reason: str,
    actor_user_id: uuid.UUID | None,
) -> PayrollPayoutWalletCorrection:
    """Перенести выбранные зарплатные расходы на фактический кошелёк одной транзакцией.

    Выплаты сотрудников и ``booked_amount`` не меняются: исправляется только источник денег.
    Ранее исключённые строки снова становятся действующими уже на правильном кошельке.
    """
    reason = reason.strip()
    if len(reason) < 3:
        raise PayrollConflictError("Укажите причину исправления (минимум 3 символа)")
    ids = tuple(dict.fromkeys(transaction_ids))
    if not ids:
        raise PayrollConflictError("Выберите хотя бы одну проводку")
    if target_wallet_code not in ALLOWED_CORRECTION_WALLETS:
        raise PayrollConflictError("Выберите Сейф или Торговую кассу Черникова")

    run = await session.scalar(select(PayrollRun).where(PayrollRun.id == run_id).with_for_update())
    if run is None:
        raise PayrollNotFoundError("Ведомость не найдена")
    if run.status != "finalized":
        raise PayrollConflictError("Исправление доступно только для финализированной ведомости")
    try:
        target_wallet = await resolve_cash_wallet(session, target_wallet_code)
    except CashWalletError as error:
        raise PayrollConflictError(str(error)) from error

    transactions = (
        await session.scalars(
            select(CashflowTransaction)
            .where(
                CashflowTransaction.id.in_(ids),
                CashflowTransaction.source_kind == PAYROLL_PAYOUT_SOURCE_KIND,
                CashflowTransaction.source_id == run_id,
                CashflowTransaction.direction == "out",
            )
            .with_for_update()
        )
    ).all()
    if len(transactions) != len(ids):
        raise PayrollConflictError("Часть выбранных проводок не относится к этой ведомости")
    source_wallet_ids = {txn.wallet_id for txn in transactions}
    if len(source_wallet_ids) != 1:
        raise PayrollConflictError("Выберите проводки только с одного исходного счёта")
    source_wallet_id = next(iter(source_wallet_ids))
    if source_wallet_id == target_wallet.id:
        raise PayrollConflictError("Проводки уже находятся на выбранном счёте")
    source_wallet = await session.get(Wallet, source_wallet_id)
    if source_wallet is None:
        raise PayrollConflictError("Исходный счёт не найден")
    if source_wallet.code not in ALLOWED_CORRECTION_WALLETS:
        raise PayrollConflictError("Исправлять можно только выплаты между Сейфом и кассой")

    previous_qualities = {str(txn.id): txn.quality_status for txn in transactions}
    total = sum((Decimal(txn.amount) for txn in transactions), Decimal("0"))
    for txn in transactions:
        txn.wallet_id = target_wallet.id
        txn.quality_status = "final"

    settlement = await run_payment_settlement(session, run.id)
    if Decimal(run.payout_cash_total or 0) > 0:
        run.payout_cash_wallet_id = target_wallet.id
        # После полного закрытия ведомости новый исторический резерв не создаём: активные/
        # ошибочно paid-резервы ниже будут честно закрыты по фактическим проводкам.
        if not settlement.settled:
            kassa_cash = (
                Decimal(run.payout_cash_total)
                if target_wallet.code == KASSA_WALLET_CODE
                else Decimal("0")
            )
            await ensure_run_kassa_reserve(
                session,
                run,
                cash_amount=kassa_cash,
                created_by_user_id=actor_user_id,
            )

    await session.flush()
    await reconcile_run_reserves(session, run.id, include_paid=True)
    session.add(
        PayrollRunEvent(
            run_id=run.id,
            period_id=run.period_id,
            action="payout_wallet_corrected",
            reason=reason,
            actor_user_id=actor_user_id,
            payload={
                "transaction_ids": [str(item) for item in ids],
                "source_wallet_id": str(source_wallet.id),
                "source_wallet_code": source_wallet.code,
                "source_wallet_name": source_wallet.name,
                "target_wallet_id": str(target_wallet.id),
                "target_wallet_code": target_wallet.code,
                "target_wallet_name": target_wallet.name,
                "total_amount": money_text(total),
                "previous_qualities": previous_qualities,
            },
        )
    )
    await session.commit()
    return PayrollPayoutWalletCorrection(
        run_id=run.id,
        transaction_ids=ids,
        source_wallet_id=source_wallet.id,
        source_wallet_name=source_wallet.name,
        target_wallet_id=target_wallet.id,
        target_wallet_code=target_wallet.code,
        target_wallet_name=target_wallet.name,
        total_amount=total,
    )

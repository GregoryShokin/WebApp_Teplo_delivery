"""Единый контур выдачи депозита производственнику (обычная выдача и при увольнении).

До этого модуля контур жил в двух местах — ``routes/deposits.py`` (тип операции ``payout``)
и ``routes/employees.py`` (``dismissal_payout``, своя логика баланса и списаний). Общими у
них были ровно четыре шага, и любая правка требовала синхронного редактирования обоих:
разъехавшись, они дают двойной расход или двойной транзит — ключи идемпотентности у
``book_production_deposit_payout_cashflow`` (``production_deposit_payout``) и
``book_deposit_bank_to_safe_transfer`` (``production_deposit_payout_draft``) разные, поэтому
забытый вызов не падает, а тихо задваивает деньги.

Что модуль НЕ делает — намеренно:
* не трогает ``DepositAccount.balance`` — у выдачи и увольнения разная логика остатка
  (увольнение при выдаче через ведомость оставляет сумму на счёте до финализации);
* не шлёт черновик в банк — это сетевой вызов, он идёт ПОСЛЕ commit вызывающего
  (БД — источник истины, ошибка банка не откатывает проведённую выдачу).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DepositTransaction, Wallet
from app.services import deposit_service
from app.services.banking.payout import channel_provider
from app.services.deposit_bank_draft import (
    PRODUCTION_DEPOSIT_PAYOUT_DRAFT_SOURCE_KIND,
    book_deposit_bank_to_safe_transfer,
)


@dataclass(frozen=True)
class DepositPayoutResult:
    """Что получилось на выходе — вызывающему нужно для ответа API и для черновика."""

    transaction: DepositTransaction
    payout_wallet: Wallet | None
    # 'tbank' / 'sber' — если канал банковский, вызывающий обязан ПОСЛЕ commit выписать
    # черновик; None — наличный канал, ничего слать не нужно.
    bank_provider: str | None


async def execute_deposit_payout(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    employee_full_name: str,
    amount: Decimal,
    payout_method: str,
    transaction_type: str,
    now: datetime,
    comment: str | None = None,
) -> DepositPayoutResult:
    """Провести выдачу депозита: запись в леджер + расход в ДДС + транзит банк→Сейф.

    ``transaction_type`` — ``payout`` (обычная выдача) или ``dismissal_payout`` (увольнение):
    оба входят в ``OUT_TYPES`` сверки депозитов, поведение леджера одинаковое.
    ``payout_method`` — ``cash_tk`` / ``cash_safe`` / ``bank_draft`` / ``bank_draft_sber``.

    Баланс счёта и отправку черновика в банк вызывающий делает сам — см. докстринг модуля.
    """
    transaction = deposit_service.add_transaction(
        session,
        employee_id=employee_id,
        transaction_type=transaction_type,
        amount=amount,
        now=now,
    )
    # Расход «Выдача депозита» с выбранного счёта; для банк-черновика — с Сейфа.
    payout_wallet = await deposit_service.book_production_deposit_payout_cashflow(
        session,
        transaction=transaction,
        payout_method=payout_method,
        transaction_date=now.date(),
        comment=comment,
    )
    # Банк-канал: пара проводок р/с→Сейф (деньги идут на карту Сейфа, оттуда раздаются).
    bank_provider = channel_provider(payout_method)
    if bank_provider is not None:
        await book_deposit_bank_to_safe_transfer(
            session,
            source_kind=PRODUCTION_DEPOSIT_PAYOUT_DRAFT_SOURCE_KIND,
            source_id=transaction.id,
            amount=amount,
            operation_date=now.date(),
            purpose=f"Выдача депозита {employee_full_name} (через Сейф)",
            provider=bank_provider,
        )
    return DepositPayoutResult(
        transaction=transaction,
        payout_wallet=payout_wallet,
        bank_provider=bank_provider,
    )

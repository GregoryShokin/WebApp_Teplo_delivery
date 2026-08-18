"""Единый контур НЕМЕДЛЕННОЙ (наличной) выдачи депозита производственнику (обычная и увольнение).

До этого модуля контур жил в двух местах — ``routes/deposits.py`` (тип операции ``payout``)
и ``routes/employees.py`` (``dismissal_payout``, своя логика баланса и списаний). Общими у
них были одни и те же шаги, и любая правка требовала синхронного редактирования обоих:
разъехавшись, они дают двойной расход депозита — ключ идемпотентности
``book_production_deposit_payout_cashflow`` — ``('production_deposit_payout', transaction.id)``,
забытый вызов в одном из мест не падает, а тихо задваивает деньги.

Только наличные каналы (``cash_tk`` / ``cash_safe``). Банк-каналы (``bank_draft`` /
``bank_draft_sber``) сюда НЕ заходят — они идут полным циклом через ``deposit_bank_draft``
(черновик в банке → оплата вебхуком/поллингом → транзит р/с→Сейф + резерв → выдача резерва),
и депозит там списывается лишь при фактической выдаче.

Что модуль НЕ делает — намеренно:
* не трогает ``DepositAccount.balance`` — у выдачи и увольнения разная логика остатка
  (увольнение при выдаче через ведомость оставляет сумму на счёте до финализации).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DepositTransaction, Wallet
from app.services import deposit_service
from app.services.deposit_schedule import assert_no_payroll_deposit_payout


@dataclass(frozen=True)
class DepositPayoutResult:
    """Что получилось на выходе — вызывающему нужно для ответа API (и iiko-изъятия)."""

    transaction: DepositTransaction
    payout_wallet: Wallet | None


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
    """Провести НЕМЕДЛЕННУЮ (наличную) выдачу депозита: запись в леджер + расход в ДДС.

    ``transaction_type`` — ``payout`` (обычная выдача) или ``dismissal_payout`` (увольнение):
    оба входят в ``OUT_TYPES`` сверки депозитов, поведение леджера одинаковое.
    ``payout_method`` — только наличные ``cash_tk`` / ``cash_safe``. Банк-каналы
    (``bank_draft`` / ``bank_draft_sber``) идут полным циклом через ``deposit_bank_draft``
    (черновик → оплата → резерв → выдача), а не через эту функцию: там депозит списывается лишь
    при фактической выдаче, а транзит банк→Сейф и резерв заводит ``apply_deposit_draft_status``.

    Баланс счёта вызывающий обнуляет сам — см. докстринг модуля.
    """
    await assert_no_payroll_deposit_payout(session, employee_id)
    transaction = deposit_service.add_transaction(
        session,
        employee_id=employee_id,
        transaction_type=transaction_type,
        amount=amount,
        now=now,
    )
    # Расход «Выдача депозита» с выбранного наличного счёта (ТК Черникова / Сейф).
    payout_wallet = await deposit_service.book_production_deposit_payout_cashflow(
        session,
        transaction=transaction,
        payout_method=payout_method,
        transaction_date=now.date(),
        comment=comment,
    )
    return DepositPayoutResult(
        transaction=transaction,
        payout_wallet=payout_wallet,
    )

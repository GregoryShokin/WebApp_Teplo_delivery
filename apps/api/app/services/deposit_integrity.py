"""Инвариант депозитного баланса производственников.

``deposit_account.balance`` обязан равняться авторитетной сумме леджера
``deposit_transaction``: ручные операции (``run_id IS NULL``) + строки ФИНАЛИЗИРОВАННЫХ
прогонов. Превью незакрытых (draft/completed) прогонов в счёт НЕ идут — их баланс двигает
только finalize. ``accrual`` складывается, ``payout/write_off/dismissal_*`` вычитаются.

Расхождение = дрейф баланса (например от повторных finalize/unfinalize — reopen-churn) или
незачищенный двойной счёт миграции. Функции только читают; лечит скрипт
``app.scripts.check_deposit_balance_integrity --heal`` / реконсиляция.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DepositAccount, DepositTransaction, Employee, PayrollRun

MONEY = Decimal("0.01")
OUT_TYPES = frozenset({"payout", "write_off", "dismissal_payout", "dismissal_writeoff"})


def _d(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def ledger_delta(transaction_type: str, amount: Any) -> Decimal:
    """Вклад одной строки леджера в баланс: accrual +, выплата/списание −, прочее 0."""
    value = _d(amount)
    if transaction_type == "accrual":
        return value
    if transaction_type in OUT_TYPES:
        return -value
    return Decimal("0")


@dataclass(frozen=True)
class BalanceDrift:
    employee_id: uuid.UUID
    name: str
    balance: Decimal
    expected: Decimal

    @property
    def diff(self) -> Decimal:
        return (self.balance - self.expected).quantize(MONEY)


async def expected_balances(session: AsyncSession) -> dict[uuid.UUID, Decimal]:
    """Ожидаемый balance по каждому сотруднику из авторитетного леджера."""
    rows = (
        await session.execute(
            select(
                DepositTransaction.employee_id,
                DepositTransaction.transaction_type,
                DepositTransaction.amount,
                DepositTransaction.run_id,
                PayrollRun.status,
            ).outerjoin(PayrollRun, PayrollRun.id == DepositTransaction.run_id)
        )
    ).all()
    totals: dict[uuid.UUID, Decimal] = {}
    for row in rows:
        # Ручные операции и финализированные прогоны авторитетны; превью — нет.
        authoritative = row.run_id is None or row.status == "finalized"
        if not authoritative:
            continue
        totals[row.employee_id] = totals.get(row.employee_id, Decimal("0")) + ledger_delta(
            row.transaction_type, row.amount
        )
    return totals


async def find_deposit_balance_drift(session: AsyncSession) -> list[BalanceDrift]:
    """Счета, где ``balance`` разошёлся с авторитетной суммой леджера."""
    accounts = (await session.scalars(select(DepositAccount))).all()
    names = {e.id: e.full_name for e in (await session.scalars(select(Employee))).all()}
    totals = await expected_balances(session)
    drift: list[BalanceDrift] = []
    for account in accounts:
        expected = totals.get(account.employee_id, Decimal("0")).quantize(MONEY)
        balance = _d(account.balance).quantize(MONEY)
        if balance != expected:
            drift.append(
                BalanceDrift(
                    employee_id=account.employee_id,
                    name=names.get(account.employee_id, str(account.employee_id)),
                    balance=balance,
                    expected=expected,
                )
            )
    return drift

"""Разовые выплаты сотрудникам (леджер ``EmployeePayout``) — общий контур.

Единый сервис создания выплат: используется и «Включить в выплату» в ведомости (гашение
долга ЗП собственника, режим оклада ``on_demand``), и плавающей кнопкой «Создать выплату
сотруднику». Наличная/сейфовая выплата проводится сразу прямой out-проводкой ДДС; банковская
(T-Bank/ИП) — отдельным контуром с черновиком и транзитом на Сейф (Трек B, добавляется позже).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CashflowTransaction, DdsArticle, Employee, EmployeePayout, Wallet
from app.services.payroll_calculator import decimal
from app.services.payroll_runner import PayrollConflictError, PayrollNotFoundError

_CENTS = Decimal("0.01")

# Денежный факт выплаты (out-проводка) помечается этим source_kind, source_id = payout.id.
EMPLOYEE_PAYOUT_SOURCE_KIND = "employee_payout"
# Статья ДДС по умолчанию для выплаты ЗП собственника.
OWNER_SALARY_ARTICLE_CODE = "zarplata_sobstvennika"
EMPLOYEE_PAYOUT_KIND_OWNER_SALARY = "owner_salary"
# owner_salary — гашение долга ЗП собственника (on_demand); salary — разовая ЗП; other — прочее.
ALLOWED_PAYOUT_KINDS = (EMPLOYEE_PAYOUT_KIND_OWNER_SALARY, "salary", "other")

# Наличные/подотчётные счета: баланс из журнала → прямая проводка выплаты допустима.
# Банковские счета (type bank/bank_account) — только через черновик+транзит на Сейф (Трек B),
# иначе прямая проводка исказит баланс банка, который ведётся от выписки.
CASH_PAYOUT_WALLET_TYPES = (
    "store_cash",
    "cash_safe",
    "cash",
    "cash_register",
    "reserve",
)


async def create_cash_employee_payout(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    amount,
    wallet_id: uuid.UUID,
    payout_date: date,
    kind: str = EMPLOYEE_PAYOUT_KIND_OWNER_SALARY,
    article_code: str = OWNER_SALARY_ARTICLE_CODE,
    note: str | None = None,
    created_by_user_id: uuid.UUID | None = None,
) -> EmployeePayout:
    """Разовая выплата сотруднику наличными/с подотчётного счёта (прямая out-проводка).

    Деньги уходят сразу (``status='paid'``): создаётся ``EmployeePayout`` и out-
    ``CashflowTransaction`` на выбранном наличном/сейфовом кошельке со статьёй ДДС (по
    умолчанию «Зарплата собственника»), связанные через ``cashflow_transaction_id``.
    Банковские счета отклоняются (нужен контур черновик+Сейф из Трека B).

    Возвращает созданный ``EmployeePayout``. Не коммитит — коммит на вызывающем.
    """
    amount = decimal(amount).quantize(_CENTS)
    if amount <= 0:
        raise PayrollConflictError("Сумма выплаты должна быть больше нуля")
    if kind not in ALLOWED_PAYOUT_KINDS:
        raise PayrollConflictError(f"Недопустимый тип выплаты: {kind}")

    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise PayrollNotFoundError("Сотрудник не найден")

    wallet = await session.get(Wallet, wallet_id)
    if wallet is None:
        raise PayrollNotFoundError("Счёт не найден")
    if wallet.status != "active":
        raise PayrollConflictError("Счёт неактивен")
    if wallet.type not in CASH_PAYOUT_WALLET_TYPES:
        raise PayrollConflictError(
            "С банковского счёта выплата проводится через черновик и Сейф — "
            "используйте «Создать выплату сотруднику» из плавающей кнопки"
        )

    article_id = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == article_code)
    )

    payout = EmployeePayout(
        id=uuid.uuid4(),
        employee_id=employee_id,
        kind=kind,
        amount=amount,
        payout_date=payout_date,
        wallet_id=wallet.id,
        article_id=article_id,
        status="paid",
        note=(note.strip() if isinstance(note, str) and note.strip() else None),
        created_by_user_id=created_by_user_id,
    )
    session.add(payout)
    await session.flush()

    transaction = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=amount,
        operation_date=payout_date,
        article_id=article_id,
        source_kind=EMPLOYEE_PAYOUT_SOURCE_KIND,
        source_id=payout.id,
        payment_purpose=f"Выплата сотруднику — {employee.full_name}",
        quality_status="final",
    )
    session.add(transaction)
    await session.flush()

    payout.cashflow_transaction_id = transaction.id
    await session.flush()
    return payout

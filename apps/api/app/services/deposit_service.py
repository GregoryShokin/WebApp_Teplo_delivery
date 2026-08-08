from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor
from app.models import (
    AgentAction,
    AgentRun,
    CashflowTransaction,
    DdsArticle,
    DepositAccount,
    DepositTransaction,
    PayrollRun,
    Wallet,
)
from app.services.clock import MOSCOW_TZ
from app.services.payroll_calculator import decimal
from app.services.wallets import SAFE_WALLET_CODE

MONEY = Decimal("0.01")

# Выдача производственного депозита — реальная выдача денег сотруднику. По умолчанию
# наличными с ТК Черникова (= iiko «Главная касса», +iiko-изъятие), либо с Сейфа (без iiko).
PRODUCTION_DEPOSIT_PAYOUT_ARTICLE_CODE = "vydacha_depozita_sotrudniku"
PRODUCTION_DEPOSIT_PAYOUT_SOURCE_KIND = "production_deposit_payout"
PRODUCTION_DEPOSIT_PAYOUT_TK_WALLET_CODE = "tk_chernikova"


async def get_deposit_account(
    session: AsyncSession,
    employee_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> DepositAccount | None:
    query = select(DepositAccount).where(DepositAccount.employee_id == employee_id)
    if for_update:
        query = query.with_for_update()
    return await session.scalar(query)


async def has_imported_deposit_accruals(
    session: AsyncSession,
    employee_id: uuid.UUID,
) -> bool:
    """Есть ли у сотрудника депозит-накопления из легаси-импорта.

    Такие строки уже представляют исторический депозит по периодам, поэтому ручная установка
    начального баланса поверх них задваивает историю (см. реконсиляцию 2026-07). Гард для
    ``set_initial_deposit_balance``.
    """
    row = await session.scalar(
        select(DepositTransaction.id)
        .join(PayrollRun, PayrollRun.id == DepositTransaction.run_id)
        .where(
            DepositTransaction.employee_id == employee_id,
            DepositTransaction.transaction_type == "accrual",
            PayrollRun.is_imported_legacy.is_(True),
        )
        .limit(1)
    )
    return row is not None


async def load_accounts(
    session: AsyncSession,
    employee_ids: list[uuid.UUID],
) -> dict[uuid.UUID, DepositAccount]:
    if not employee_ids:
        return {}
    result = await session.scalars(
        select(DepositAccount).where(DepositAccount.employee_id.in_(employee_ids))
    )
    return {account.employee_id: account for account in result.all()}


def ensure_account(
    session: AsyncSession,
    employee_id: uuid.UUID,
    account: DepositAccount | None,
    now: datetime,
) -> DepositAccount:
    if account is not None:
        return account
    account = DepositAccount(
        id=uuid.uuid4(),
        employee_id=employee_id,
        balance=Decimal("0"),
        initial_balance=Decimal("0"),
        last_updated=now,
    )
    session.add(account)
    return account


def add_transaction(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    transaction_type: str,
    amount: Decimal,
    now: datetime,
    happened_on: date | None = None,
) -> DepositTransaction:
    """Строка депозитного леджера.

    ``happened_on`` — хозяйственный день события; по умолчанию день записи ПО МОСКВЕ. Момент
    записи (``created_at``) для баланса не годится: он в UTC, и всё, что заведено между
    полуночью и тремя часами ночи, уехало бы в предыдущий день, а с ним и в предыдущий месяц.
    """
    transaction = DepositTransaction(
        id=uuid.uuid4(),
        employee_id=employee_id,
        run_id=None,
        transaction_type=transaction_type,
        amount=amount,
        happened_on=happened_on or now.astimezone(MOSCOW_TZ).date(),
        created_at=now,
    )
    session.add(transaction)
    return transaction


async def book_production_deposit_payout_cashflow(
    session: AsyncSession,
    *,
    transaction: DepositTransaction,
    payout_method: str,
    transaction_date: date,
    comment: str | None,
) -> Wallet | None:
    """Провести немедленную выдачу производственного депозита в ДДС (расход с наличного счёта).

    Списание с ТК Черникова (``cash_tk``) или Сейфа (``cash_safe``/``bank_draft``) по статье
    «Выдача депозита». Идемпотентно по ``source_id = transaction.id``. Возвращает кошелёк —
    роут по нему решает, нужно ли iiko-изъятие (только для ТК Черникова = iiko «Главная
    касса»). Для банк-черновика (``bank_draft``) расход списывается с Сейфа, а транзит
    банк→Сейф и сам черновик заводятся отдельно (``deposit_bank_draft``). Устойчиво (как
    курьерский возврат): если кошелёк/статья не заведены — ДДС-проводка пропускается
    (возврат None / без записи), но выдачу не валим.
    """
    wallet_code = (
        PRODUCTION_DEPOSIT_PAYOUT_TK_WALLET_CODE
        if payout_method == "cash_tk"
        else SAFE_WALLET_CODE
    )
    wallet = await session.scalar(
        select(Wallet).where(Wallet.code == wallet_code, Wallet.status == "active")
    )
    if wallet is None:
        return None
    existing = await session.scalar(
        select(CashflowTransaction.id).where(
            CashflowTransaction.source_kind == PRODUCTION_DEPOSIT_PAYOUT_SOURCE_KIND,
            CashflowTransaction.source_id == transaction.id,
        )
    )
    if existing is not None:
        return wallet
    article_id = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == PRODUCTION_DEPOSIT_PAYOUT_ARTICLE_CODE)
    )
    if article_id is None:
        return wallet
    session.add(
        CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=transaction.amount,
            operation_date=transaction_date,
            article_id=article_id,
            source_kind=PRODUCTION_DEPOSIT_PAYOUT_SOURCE_KIND,
            source_id=transaction.id,
            payment_purpose=f"Выдача депозита сотруднику (операция {transaction.id})",
            comment=comment,
            quality_status="final",
        )
    )
    await session.flush()
    return wallet


async def add_deposit_action(
    session: AsyncSession,
    *,
    action_type: str,
    target_table: str,
    target_id: uuid.UUID,
    employee_id: uuid.UUID,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    now: datetime,
    actor: CurrentActor,
    comment: str | None = None,
    agent_name: str = "deposit_manual_change",
) -> uuid.UUID:
    agent_run = AgentRun(
        id=uuid.uuid4(),
        agent_name=agent_name,
        finished_at=now,
        status="success",
        params={
            "employee_id": str(employee_id),
            "actor_roles": sorted(actor.roles),
            "comment": comment,
        },
        result={"action_type": action_type},
    )
    session.add(agent_run)
    await session.flush()
    session.add(
        AgentAction(
            id=uuid.uuid4(),
            agent_run_id=agent_run.id,
            action_type=action_type,
            target_table=target_table,
            target_id=target_id,
            before_value=before,
            after_value=after,
        )
    )
    return agent_run.id


def deposit_account_snapshot(account: DepositAccount | None) -> dict[str, Any] | None:
    if account is None:
        return None
    return {
        "id": str(account.id),
        "employee_id": str(account.employee_id),
        "balance": decimal_string(account.balance),
        "initial_balance": decimal_string(getattr(account, "initial_balance", 0)),
        "last_updated": account.last_updated.isoformat() if account.last_updated else None,
    }


def transaction_payload(transaction: DepositTransaction) -> dict[str, Any]:
    return {
        "id": str(transaction.id) if transaction.id is not None else None,
        "employee_id": str(transaction.employee_id)
        if transaction.employee_id is not None
        else None,
        "run_id": str(transaction.run_id) if transaction.run_id is not None else None,
        "transaction_type": transaction.transaction_type,
        "amount": decimal_string(transaction.amount),
        "created_at": (
            transaction.created_at.isoformat() if transaction.created_at is not None else None
        ),
    }


def decimal_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(decimal(value).quantize(MONEY))


def date_string(value: date | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()

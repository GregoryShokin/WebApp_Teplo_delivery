from __future__ import annotations

import uuid
from datetime import datetime, date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor
from app.models import AgentAction, AgentRun, DepositAccount, DepositTransaction
from app.services.payroll_calculator import decimal

MONEY = Decimal("0.01")


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
) -> DepositTransaction:
    transaction = DepositTransaction(
        id=uuid.uuid4(),
        employee_id=employee_id,
        run_id=None,
        transaction_type=transaction_type,
        amount=amount,
        created_at=now,
    )
    session.add(transaction)
    return transaction


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
        "employee_id": str(transaction.employee_id) if transaction.employee_id is not None else None,
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

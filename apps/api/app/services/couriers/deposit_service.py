from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal

from fastapi import HTTPException, status
from sqlalchemy import desc, select
from sqlalchemy import update as sqlalchemy_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AppSetting,
    AppSettingHistory,
    CourierDepositAccount,
    CourierDepositTransaction,
    CourierDepositTransactionType,
    Employee,
)
from app.services.couriers.common import get_courier_or_404

DepositStatusFilter = Literal["active", "fired", "all"]
DepositCategoryFilter = Literal["primary", "secondary", "all"]

DEPOSIT_SETTING_KEYS = {
    "target_amount": "couriers.deposit.target_amount",
    "withhold_primary": "couriers.deposit.withhold_primary",
    "withhold_secondary": "couriers.deposit.withhold_secondary",
    "auto_withhold_enabled": "couriers.deposit.auto_withhold_enabled",
}
DEFAULT_DEPOSIT_SETTINGS: dict[str, int | bool] = {
    "target_amount": 5000,
    "withhold_primary": 500,
    "withhold_secondary": 200,
    "auto_withhold_enabled": False,
}


@dataclass(frozen=True)
class CourierDepositFilters:
    status: DepositStatusFilter = "active"
    category: DepositCategoryFilter = "all"


async def get_deposit_settings(session: AsyncSession) -> dict[str, int | bool]:
    result = await session.scalars(
        select(AppSetting).where(AppSetting.key.in_(DEPOSIT_SETTING_KEYS.values()))
    )
    values = dict(DEFAULT_DEPOSIT_SETTINGS)
    reverse = {setting_key: name for name, setting_key in DEPOSIT_SETTING_KEYS.items()}
    for setting in result.all():
        name = reverse.get(setting.key)
        if name is not None:
            values[name] = setting.value
    return values


async def update_deposit_settings(
    session: AsyncSession,
    values: dict[str, int | bool],
    changed_by_user_id: uuid.UUID | None,
) -> dict[str, int | bool]:
    settings_by_key = {
        setting.key: setting
        for setting in (
            await session.scalars(
                select(AppSetting).where(AppSetting.key.in_(DEPOSIT_SETTING_KEYS.values()))
            )
        ).all()
    }
    now = datetime.now(UTC)
    for name, value in values.items():
        if name not in DEPOSIT_SETTING_KEYS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unsupported setting: {name}",
            )
        if name == "auto_withhold_enabled":
            if not isinstance(value, bool):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"{name} must be boolean",
                )
        elif not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{name} must be a non-negative integer",
            )

        setting = settings_by_key.get(DEPOSIT_SETTING_KEYS[name])
        if setting is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Setting {DEPOSIT_SETTING_KEYS[name]} was not found",
            )
        old_value = setting.value
        setting.value = value
        setting.updated_at = now
        setting.updated_by_user_id = changed_by_user_id
        if name == "target_amount":
            await session.execute(
                sqlalchemy_update(CourierDepositAccount).values(
                    target_amount_cents=_rub_to_cents(value),
                    updated_at=now,
                )
            )
        session.add(
            AppSettingHistory(
                setting_id=setting.id,
                old_value=old_value,
                new_value=value,
                changed_by_user_id=changed_by_user_id,
            )
        )
    await session.flush()
    return await get_deposit_settings(session)


async def ensure_account(
    session: AsyncSession,
    employee_id: uuid.UUID,
) -> CourierDepositAccount:
    await get_courier_or_404(session, employee_id)
    account = await session.get(CourierDepositAccount, employee_id)
    if account is not None:
        return account

    settings = await get_deposit_settings(session)
    account = CourierDepositAccount(
        employee_id=employee_id,
        target_amount_cents=_rub_to_cents(settings["target_amount"]),
        opening_balance_cents=0,
        opening_date=date.today(),
    )
    session.add(account)
    await session.flush()
    return account


async def set_opening_balance(
    session: AsyncSession,
    employee_id: uuid.UUID,
    amount_cents: int,
    opening_date: date,
) -> CourierDepositAccount:
    if amount_cents < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="opening_balance must be non-negative",
        )
    account = await ensure_account(session, employee_id)
    account.opening_balance_cents = amount_cents
    account.opening_date = opening_date
    account.updated_at = datetime.now(UTC)
    await session.flush()
    return account


async def create_transaction(
    session: AsyncSession,
    employee_id: uuid.UUID,
    transaction_type: CourierDepositTransactionType | str,
    amount_cents: int,
    transaction_date: date,
    comment: str | None,
    created_by_user_id: uuid.UUID,
) -> CourierDepositTransaction:
    if amount_cents <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="amount_cents must be greater than 0",
        )
    account = await ensure_account(session, employee_id)
    transaction = CourierDepositTransaction(
        account_employee_id=account.employee_id,
        transaction_type=_transaction_type_value(transaction_type),
        amount_cents=amount_cents,
        transaction_date=transaction_date,
        comment=comment,
        created_by=created_by_user_id,
    )
    account.updated_at = datetime.now(UTC)
    session.add(transaction)
    await session.flush()
    return transaction


async def get_balance(
    session: AsyncSession,
    employee_id: uuid.UUID,
    at_date: date | None = None,
) -> int:
    account = await session.get(CourierDepositAccount, employee_id)
    if account is None:
        return 0

    balance_date = at_date or date.today()
    transactions = (
        await session.scalars(
            select(CourierDepositTransaction).where(
                CourierDepositTransaction.account_employee_id == employee_id,
                CourierDepositTransaction.transaction_date <= balance_date,
            )
        )
    ).all()
    balance = account.opening_balance_cents
    for transaction in transactions:
        if _enum_value(transaction.transaction_type) == CourierDepositTransactionType.TOP_UP.value:
            balance += transaction.amount_cents
        else:
            balance -= transaction.amount_cents
    return balance


async def list_couriers_with_balances(
    session: AsyncSession,
    filters: CourierDepositFilters | None = None,
) -> list[dict[str, Any]]:
    resolved_filters = filters or CourierDepositFilters()
    result = await session.scalars(
        select(Employee).where(Employee.position == "Курьер").order_by(Employee.full_name)
    )
    rows: list[dict[str, Any]] = []
    for employee in result.all():
        if not _matches_status(employee, resolved_filters.status):
            continue

        account = await ensure_account(session, employee.id)
        balance = await get_balance(session, employee.id)
        last_transaction = await _last_transaction(session, employee.id)
        target = account.target_amount_cents
        rows.append(
            {
                "employee_id": employee.id,
                "full_name": employee.full_name,
                "status": employee.status,
                "category": None,
                "target_amount_cents": target,
                "opening_balance_cents": account.opening_balance_cents,
                "opening_date": account.opening_date,
                "balance_cents": balance,
                "progress_pct": min(round((balance / target) * 100, 2), 100) if target else 0,
                "remaining_to_target_cents": max(target - balance, 0),
                "last_transaction": (
                    transaction_payload(last_transaction) if last_transaction is not None else None
                ),
            }
        )
    await session.flush()
    return rows


def account_payload(account: CourierDepositAccount) -> dict[str, Any]:
    return {
        "employee_id": account.employee_id,
        "target_amount_cents": account.target_amount_cents,
        "opening_balance_cents": account.opening_balance_cents,
        "opening_date": account.opening_date,
        "created_at": account.created_at,
        "updated_at": account.updated_at,
    }


def transaction_payload(
    transaction: CourierDepositTransaction,
    created_by_name: str | None = None,
) -> dict[str, Any]:
    return {
        "id": transaction.id,
        "account_employee_id": transaction.account_employee_id,
        "transaction_type": _enum_value(transaction.transaction_type),
        "amount_cents": transaction.amount_cents,
        "transaction_date": transaction.transaction_date,
        "comment": transaction.comment,
        "created_by": transaction.created_by,
        "created_by_name": created_by_name,
        "created_at": transaction.created_at,
    }


async def list_transactions(
    session: AsyncSession,
    employee_id: uuid.UUID,
) -> list[CourierDepositTransaction]:
    return list(
        (
            await session.scalars(
                select(CourierDepositTransaction)
                .where(CourierDepositTransaction.account_employee_id == employee_id)
                .order_by(
                    CourierDepositTransaction.transaction_date.desc(),
                    CourierDepositTransaction.id.desc(),
                )
            )
        ).all()
    )


async def _last_transaction(
    session: AsyncSession,
    employee_id: uuid.UUID,
) -> CourierDepositTransaction | None:
    return await session.scalar(
        select(CourierDepositTransaction)
        .where(CourierDepositTransaction.account_employee_id == employee_id)
        .order_by(
            desc(CourierDepositTransaction.transaction_date),
            desc(CourierDepositTransaction.id),
        )
        .limit(1)
    )


def _matches_status(employee: Employee, status_filter: DepositStatusFilter) -> bool:
    if status_filter == "all":
        return True
    is_active = employee.status == "active" and employee.fire_date is None
    if status_filter == "active":
        return is_active
    return not is_active


def _rub_to_cents(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Deposit money setting must be numeric",
        )
    return int(round(float(value) * 100))


def _transaction_type_value(
    transaction_type: CourierDepositTransactionType | str,
) -> CourierDepositTransactionType:
    if isinstance(transaction_type, CourierDepositTransactionType):
        return transaction_type
    return CourierDepositTransactionType(transaction_type)


def _enum_value(value: Any) -> str | None:
    if value is None:
        return None
    return getattr(value, "value", value)

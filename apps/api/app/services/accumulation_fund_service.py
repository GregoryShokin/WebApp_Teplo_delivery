from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccumulationFundAccount, AccumulationFundTransaction, Employee
from app.services.clock import MOSCOW_TZ
from app.services.payroll_calculator import decimal
from app.services.position_registry import production_payroll_positions

MONEY = Decimal("0.01")


@dataclass(slots=True)
class FundPayoutResult:
    year: int
    paid_out_count: int
    total_paid_out: Decimal
    forfeited_count: int = 0
    total_forfeited: Decimal = Decimal("0")


def fund_outstanding(account: AccumulationFundAccount | None) -> Decimal:
    if account is None:
        return Decimal("0")
    return (
        decimal(account.accumulated_amount)
        - decimal(account.paid_out_amount)
        - decimal(getattr(account, "forfeited_amount", 0))
    )


def fund_account_visible_in_roster(
    account: AccumulationFundAccount,
    employee: Employee,
) -> bool:
    """Keep current staff and unfinished forfeitures in the operational roster."""
    if account.settled_at is not None:
        return False
    if employee.status == "active" and employee.fire_date is None:
        return True
    return any(
        decimal(amount) > 0
        for amount in (
            account.accumulated_amount,
            account.paid_out_amount,
            account.forfeited_amount,
        )
    )


def forfeit_fund_account(
    session: AsyncSession,
    account: AccumulationFundAccount,
    *,
    now: datetime,
    comment: str,
    happened_on: date | None = None,
) -> Decimal:
    """Сгорание фонда. ``happened_on`` — день события (при увольнении это дата увольнения).

    Без него баланс на дату считал бы фонд по ТЕКУЩЕМУ статусу счёта: сотрудник, уволенный в
    сентябре, задним числом лишался бы фонда и в августовском срезе, где долг перед ним был жив.
    """
    outstanding = fund_outstanding(account)
    if outstanding <= 0:
        if outstanding == 0 and decimal(account.accumulated_amount) > 0:
            account.status = "forfeited"
            account.forfeited_at = now
            account.forfeit_reason = comment
            account.settled_at = None
        return Decimal("0")

    account.forfeited_amount = decimal(account.forfeited_amount) + outstanding
    account.status = "forfeited"
    account.forfeited_at = now
    account.forfeit_reason = comment
    account.settled_at = None
    session.add(
        AccumulationFundTransaction(
            id=uuid.uuid4(),
            account_id=account.id,
            employee_id=account.employee_id,
            year=account.year,
            run_id=None,
            transaction_type="forfeit",
            amount=outstanding,
            comment=comment,
            happened_on=happened_on or now.astimezone(MOSCOW_TZ).date(),
            created_at=now,
        )
    )
    return outstanding


async def forfeit_active_fund_on_dismiss(
    session: AsyncSession,
    employee: Employee,
    *,
    fire_date: date,
    now: datetime,
) -> None:
    if employee.position not in production_payroll_positions():
        return
    result = await session.scalars(
        select(AccumulationFundAccount)
        .where(
            AccumulationFundAccount.employee_id == employee.id,
            AccumulationFundAccount.status == "active",
        )
        .with_for_update()
    )
    for account in result.all():
        forfeit_fund_account(
            session,
            account,
            now=now,
            comment=f"Увольнение {fire_date.isoformat()}",
            # Дата увольнения, а не день обработки: фонд сгорает вместе с человеком.
            happened_on=fire_date,
        )


async def payout_fund_accounts_for_year(
    session: AsyncSession,
    year: int,
    *,
    run_id: uuid.UUID | None = None,
    now: datetime | None = None,
    comment: str | None = None,
    happened_on: date | None = None,
) -> FundPayoutResult:
    now = now or datetime.now(UTC)
    dismissed_result = await session.scalars(
        select(AccumulationFundAccount)
        .join(Employee, Employee.id == AccumulationFundAccount.employee_id)
        .where(
            AccumulationFundAccount.year == year,
            AccumulationFundAccount.status == "active",
            Employee.position.in_(production_payroll_positions()),
            or_(Employee.status != "active", Employee.fire_date.is_not(None)),
        )
        .with_for_update()
    )
    forfeited_total = Decimal("0")
    forfeited_count = 0
    for account in dismissed_result.all():
        forfeited = forfeit_fund_account(
            session,
            account,
            now=now,
            comment=f"Списание фонда за {year} год: сотрудник уволен до выплаты",
            happened_on=happened_on,
        )
        if forfeited > 0:
            forfeited_total += forfeited
            forfeited_count += 1

    unsettled_forfeited_result = await session.scalars(
        select(AccumulationFundAccount)
        .where(
            AccumulationFundAccount.year == year,
            AccumulationFundAccount.status == "forfeited",
            AccumulationFundAccount.settled_at.is_(None),
        )
        .with_for_update()
    )
    for account in unsettled_forfeited_result.all():
        account.settled_at = now

    result = await session.scalars(
        select(AccumulationFundAccount)
        .join(Employee, Employee.id == AccumulationFundAccount.employee_id)
        .where(
            AccumulationFundAccount.year == year,
            AccumulationFundAccount.status == "active",
            Employee.position.in_(production_payroll_positions()),
            Employee.status == "active",
            Employee.fire_date.is_(None),
        )
        .with_for_update()
    )
    total = Decimal("0")
    count = 0
    for account in result.all():
        outstanding = fund_outstanding(account)
        if outstanding <= 0:
            continue
        account.paid_out_amount = decimal(account.paid_out_amount) + outstanding
        account.status = "paid_out"
        account.paid_out_at = now
        session.add(
            AccumulationFundTransaction(
                id=uuid.uuid4(),
                account_id=account.id,
                employee_id=account.employee_id,
                year=year,
                run_id=run_id,
                transaction_type="payout",
                amount=outstanding,
                comment=comment or f"Выплата фонда за {year} год",
                happened_on=happened_on or now.astimezone(MOSCOW_TZ).date(),
                created_at=now,
            )
        )
        total += outstanding
        count += 1
    return FundPayoutResult(
        year=year,
        paid_out_count=count,
        total_paid_out=total,
        forfeited_count=forfeited_count,
        total_forfeited=forfeited_total,
    )


def decimal_string(value: Any) -> str:
    return str(decimal(value).quantize(MONEY))

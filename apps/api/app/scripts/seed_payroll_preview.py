"""Seed a clean, reproducible payroll preview dataset.

The script is intended only for an isolated local preview database. Migrations must be
applied first. It creates one finalized production payroll run and gives cash wallets
positive opening balances so payout-split scenarios do not start from an artificial deficit.

Usage:
    DATABASE_URL=postgresql+asyncpg://... python -m app.scripts.seed_payroll_preview
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models import (
    Employee,
    EmployeePositionAssignment,
    PayrollLine,
    PayrollPeriod,
    PayrollRun,
    PayrollRunEvent,
    User,
    Wallet,
)

SEED_MARKER = "payroll_reserve_preview_v1"
PERIOD_START = date(2026, 7, 7)
PERIOD_END = date(2026, 7, 13)
PAYROLL_DATE = date(2026, 7, 14)
OPENING_BALANCE_DATE = date(2026, 7, 1)

WALLET_BALANCES = {
    "tbank_main": Decimal("500000.00"),
    "sber_main": Decimal("200000.00"),
    "cash_safe": Decimal("100000.00"),
    "tk_chernikova": Decimal("80000.00"),
}


@dataclass(frozen=True, slots=True)
class PreviewEmployee:
    full_name: str
    position: str
    payroll_role: str
    category: str
    station: str | None
    total_payable: Decimal


EMPLOYEES = (
    PreviewEmployee(
        "Алексей Соколов",
        "Повар",
        "pizza",
        "category_2",
        "pizza",
        Decimal("28000.00"),
    ),
    PreviewEmployee(
        "Мария Волкова",
        "Кассир",
        "administrator",
        "category_2",
        None,
        Decimal("22000.00"),
    ),
    PreviewEmployee(
        "Дмитрий Орлов",
        "Повар",
        "sushi",
        "category_3",
        "sushi",
        Decimal("26000.00"),
    ),
    PreviewEmployee(
        "Анна Лебедева",
        "Повар",
        "prep",
        "category_1",
        None,
        Decimal("18000.00"),
    ),
)


async def _set_wallet_balances(session: AsyncSession) -> None:
    wallets = (await session.scalars(select(Wallet).where(Wallet.code.in_(WALLET_BALANCES)))).all()
    by_code = {wallet.code: wallet for wallet in wallets}
    missing = sorted(set(WALLET_BALANCES) - set(by_code))
    if missing:
        raise RuntimeError(f"Не найдены кошельки после миграций: {', '.join(missing)}")
    for code, balance in WALLET_BALANCES.items():
        wallet = by_code[code]
        wallet.opening_balance = balance
        wallet.opening_balance_date = OPENING_BALANCE_DATE


async def _existing_run(session: AsyncSession) -> PayrollRun | None:
    runs = (
        await session.scalars(
            select(PayrollRun).where(
                PayrollRun.status == "finalized",
                PayrollRun.period_id.in_(
                    select(PayrollPeriod.id).where(
                        PayrollPeriod.start_date == PERIOD_START,
                        PayrollPeriod.end_date == PERIOD_END,
                    )
                ),
            )
        )
    ).all()
    return next(
        (
            run
            for run in runs
            if isinstance(run.summary, dict) and run.summary.get("preview_seed") == SEED_MARKER
        ),
        None,
    )


def _payroll_line(run_id: uuid.UUID, employee_id: uuid.UUID, row: PreviewEmployee) -> PayrollLine:
    return PayrollLine(
        id=uuid.uuid4(),
        run_id=run_id,
        employee_id=employee_id,
        role=row.payroll_role,
        base_pay=row.total_payable,
        premium=Decimal("0.00"),
        percent_pay=Decimal("0.00"),
        vacation_pay=Decimal("0.00"),
        ndfl_withheld=Decimal("0.00"),
        fund_accrual=Decimal("0.00"),
        deduction=Decimal("0.00"),
        total_payable=row.total_payable,
        deposit_excluded_for_run=False,
        deposit_exclusion_reason=None,
        components={"days": [], "adjustments": {}, "preview_seed": SEED_MARKER},
    )


async def seed_preview(session: AsyncSession) -> PayrollRun:
    await _set_wallet_balances(session)
    existing = await _existing_run(session)
    if existing is not None:
        await session.commit()
        return existing

    actor = await session.scalar(select(User).where(User.email == "admin1@teplo.local"))
    if actor is None:
        actor = await session.scalar(select(User).order_by(User.created_at, User.id))
    if actor is None:
        raise RuntimeError("Не найден пользователь администратора после миграций")

    now = datetime.now(UTC)
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="week",
        start_date=PERIOD_START,
        end_date=PERIOD_END,
        payroll_date=PAYROLL_DATE,
        status="finalized",
        finalized_at=now,
        finalized_by_user_id=actor.id,
    )
    total_payable = sum((row.total_payable for row in EMPLOYEES), Decimal("0.00"))
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=period.id,
        started_at=now,
        finished_at=now,
        status="finalized",
        blocking_issues=[],
        summary={
            "preview_seed": SEED_MARKER,
            "employee_count": len(EMPLOYEES),
            "total_payable": float(total_payable),
            "payment_state": "unpaid",
            "paid_total": 0,
            "remaining_shortfall": float(total_payable),
            "underpaid_count": len(EMPLOYEES),
        },
        is_imported_legacy=False,
        payout_cash_total=Decimal("0.00"),
        payout_cash_wallet_id=None,
    )
    session.add_all([period, run])
    await session.flush()

    for index, row in enumerate(EMPLOYEES, start=1):
        employee = Employee(
            id=uuid.uuid4(),
            full_name=row.full_name,
            iiko_id=f"preview-payroll-{index}",
            category=row.category,
            default_cooking_station=row.station,
            status="active",
            is_senior=False,
            is_deputy_senior=False,
            pin_hash=f"preview-pin-{index}",
            pin_set_at=now,
            hire_date=date(2025, 1, index),
            tenure_started_at=date(2025, 1, index),
            created_at=now,
            updated_at=now,
        )
        session.add(employee)
        await session.flush()
        session.add(
            EmployeePositionAssignment(
                id=uuid.uuid4(),
                employee_id=employee.id,
                position=row.position,
                effective_from=date(2025, 1, index),
                effective_to=None,
                comment="Демо-данные превью зарплатной ведомости",
                created_by_user_id=actor.id,
            )
        )
        session.add(_payroll_line(run.id, employee.id, row))

    session.add(
        PayrollRunEvent(
            run_id=run.id,
            period_id=period.id,
            action="finalized",
            reason="Демо-данные локального превью",
            actor_user_id=actor.id,
            payload={"preview_seed": SEED_MARKER, "total_payable": str(total_payable)},
        )
    )
    await session.commit()
    return run


async def _main() -> None:
    async with AsyncSessionLocal() as session:
        run = await seed_preview(session)
        print(f"Preview payroll run: {run.id}")
        for code, balance in WALLET_BALANCES.items():
            print(f"  {code}: {balance:.2f}")


if __name__ == "__main__":
    asyncio.run(_main())

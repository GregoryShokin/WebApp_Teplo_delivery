"""Seed a finalized ADMIN payroll run for local UI preview.

Only for an isolated local preview database (migrations applied first). Creates one
finalized half-month admin run with a few lines — an okladnik, a manager with bonus/
penalty adjustments, a cleaner and a dishwasher — so the payout dialog, the DDS split
and the per-row detail drill-down have realistic data. Cash/bank wallets get positive
opening balances so the payout modal is not stuck on an artificial deficit.

Usage:
    DATABASE_URL=postgresql+asyncpg://... python -m app.scripts.seed_admin_payroll_preview
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
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

SEED_MARKER = "admin_payroll_preview_v1"
PERIOD_START = date(2026, 7, 1)
PERIOD_END = date(2026, 7, 15)
PAYROLL_DATE = date(2026, 7, 15)
OPENING_BALANCE_DATE = date(2026, 7, 1)

WALLET_BALANCES = {
    "tbank_main": Decimal("500000.00"),
    "sber_main": Decimal("200000.00"),
    "cash_safe": Decimal("100000.00"),
    "tk_chernikova": Decimal("80000.00"),
}


@dataclass(frozen=True, slots=True)
class PreviewLine:
    full_name: str
    position: str
    base_pay: Decimal
    premium: Decimal
    deduction: Decimal
    total_payable: Decimal
    components: dict = field(default_factory=dict)


LINES = (
    PreviewLine(
        "Степовой Юрий",
        "Управляющий",
        Decimal("45000.00"),
        Decimal("0.00"),
        Decimal("0.00"),
        Decimal("45000.00"),
        {"days": [], "adjustments": {}, "kind": "admin_oklad"},
    ),
    PreviewLine(
        "Ирина Менеджерова",
        "Менеджер",
        Decimal("30000.00"),
        Decimal("5000.00"),
        Decimal("2000.00"),
        Decimal("33000.00"),
        {
            "days": [],
            "kind": "admin_oklad",
            "adjustments": {
                "bonuses": [
                    {
                        "id": "seed-bonus-1",
                        "work_date": "2026-07-10",
                        "category": "Премия за смену",
                        "amount": 5000,
                        "comment": "Перевыполнение плана",
                    }
                ],
                "penalties": [
                    {
                        "id": "seed-penalty-1",
                        "work_date": "2026-07-12",
                        "category": "Штраф — опоздание",
                        "amount": 2000,
                        "comment": "Опоздание на открытие",
                    }
                ],
            },
        },
    ),
    PreviewLine(
        "Мастер чистоты Черникова",
        "Уборщица",
        Decimal("15000.00"),
        Decimal("0.00"),
        Decimal("0.00"),
        Decimal("15000.00"),
        {"days": [], "adjustments": {}, "kind": "admin_oklad"},
    ),
    PreviewLine(
        "Мойщица Василиса",
        "Посудомойка",
        Decimal("3500.00"),
        Decimal("0.00"),
        Decimal("0.00"),
        Decimal("3500.00"),
        {"days": [], "adjustments": {}, "kind": "dishwasher_shifts", "shifts": 7},
    ),
)


async def _set_wallet_balances(session: AsyncSession) -> None:
    wallets = (await session.scalars(select(Wallet).where(Wallet.code.in_(WALLET_BALANCES)))).all()
    by_code = {wallet.code: wallet for wallet in wallets}
    for code, balance in WALLET_BALANCES.items():
        wallet = by_code.get(code)
        if wallet is not None:
            wallet.opening_balance = balance
            wallet.opening_balance_date = OPENING_BALANCE_DATE


async def _existing_run(session: AsyncSession) -> PayrollRun | None:
    runs = (
        await session.scalars(
            select(PayrollRun).where(
                PayrollRun.period_id.in_(
                    select(PayrollPeriod.id).where(
                        PayrollPeriod.period_type == "half_month",
                        PayrollPeriod.start_date == PERIOD_START,
                        PayrollPeriod.end_date == PERIOD_END,
                    )
                )
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
        period_type="half_month",
        start_date=PERIOD_START,
        end_date=PERIOD_END,
        payroll_date=PAYROLL_DATE,
        status="finalized",
        finalized_at=now,
        finalized_by_user_id=actor.id,
    )
    total_payable = sum((row.total_payable for row in LINES), Decimal("0.00"))
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=period.id,
        started_at=now,
        finished_at=now,
        status="finalized",
        blocking_issues=[],
        summary={
            "preview_seed": SEED_MARKER,
            "kind": "admin",
            "employee_count": len(LINES),
            "total_payable": float(total_payable),
            "excluded_no_oklad": [],
            "excluded_wrong_position": [],
        },
        is_imported_legacy=False,
        payout_cash_total=Decimal("0.00"),
        payout_cash_wallet_id=None,
    )
    session.add_all([period, run])
    await session.flush()

    for index, row in enumerate(LINES, start=1):
        employee = Employee(
            id=uuid.uuid4(),
            full_name=row.full_name,
            iiko_id=f"preview-admin-{index}",
            category="category_1",
            default_cooking_station=None,
            status="active",
            is_senior=False,
            is_deputy_senior=False,
            pin_hash=f"preview-admin-pin-{index}",
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
                comment="Демо-данные превью административной ведомости",
                created_by_user_id=actor.id,
            )
        )
        session.add(
            PayrollLine(
                id=uuid.uuid4(),
                run_id=run.id,
                employee_id=employee.id,
                role=row.position,
                base_pay=row.base_pay,
                premium=row.premium,
                percent_pay=Decimal("0.00"),
                vacation_pay=Decimal("0.00"),
                ndfl_withheld=Decimal("0.00"),
                fund_accrual=Decimal("0.00"),
                deduction=row.deduction,
                total_payable=row.total_payable,
                deposit_excluded_for_run=False,
                deposit_exclusion_reason=None,
                components={**row.components, "preview_seed": SEED_MARKER},
            )
        )

    session.add(
        PayrollRunEvent(
            run_id=run.id,
            period_id=period.id,
            action="finalized",
            reason="Демо-данные локального превью (администрация)",
            actor_user_id=actor.id,
            payload={"preview_seed": SEED_MARKER, "total_payable": str(total_payable)},
        )
    )
    await session.commit()
    return run


async def _main() -> None:
    async with AsyncSessionLocal() as session:
        run = await seed_preview(session)
        print(f"Admin payroll preview run: {run.id}")


if __name__ == "__main__":
    asyncio.run(_main())

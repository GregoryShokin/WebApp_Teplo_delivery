from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    DepositAccount,
    DepositTransaction,
    Employee,
    PayrollPeriod,
    PayrollRun,
)
from app.services import deposit_service
from app.services.deposit_integrity import expected_balances, find_deposit_balance_drift

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _employee() -> Employee:
    return Employee(
        id=uuid.uuid4(),
        full_name="Integrity Employee",
        iiko_id=f"iiko-{uuid.uuid4()}",
        category="category_2",
        status="active",
        is_senior=False,
        is_deputy_senior=False,
        pin_hash="hashed-pin",
        pin_set_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


async def _run(
    session: AsyncSession, *, status: str, is_legacy: bool = False, week: int = 0
) -> PayrollRun:
    start = date(2026, 5, 19) + timedelta(days=7 * week)
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="week",
        start_date=start,
        end_date=start + timedelta(days=6),
        payroll_date=start + timedelta(days=7),
        status="finalized" if status == "finalized" else "open",
    )
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=period.id,
        started_at=NOW,
        finished_at=NOW,
        status=status,
        blocking_issues=[],
        summary={},
        is_imported_legacy=is_legacy,
    )
    session.add_all([period, run])
    await session.flush()
    return run


def _tx(employee_id, ttype, amount, run_id=None) -> DepositTransaction:
    return DepositTransaction(
        id=uuid.uuid4(),
        employee_id=employee_id,
        run_id=run_id,
        transaction_type=ttype,
        amount=Decimal(amount),
        created_at=NOW,
    )


@pytest.mark.asyncio
async def test_expected_balance_counts_manual_and_finalized_only(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        emp = _employee()
        session.add(emp)
        await session.flush()
        final_run = await _run(session, status="finalized", week=0)
        draft_run = await _run(session, status="completed", week=1)
        session.add_all(
            [
                _tx(emp.id, "accrual", "4000"),  # ручной initial (run_id NULL) → в счёт
                _tx(emp.id, "accrual", "1000", final_run.id),  # финализирован → в счёт
                _tx(emp.id, "accrual", "500", draft_run.id),  # превью draft → НЕ в счёт
                _tx(emp.id, "payout", "200"),  # ручная выплата → минус
            ]
        )
        await session.commit()

        totals = await expected_balances(session)
        assert totals[emp.id] == Decimal("4800")  # 4000 + 1000 - 200, draft 500 не учтён


@pytest.mark.asyncio
async def test_find_drift_flags_and_clears(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        emp = _employee()
        session.add(emp)
        await session.flush()
        run = await _run(session, status="finalized")
        session.add(_tx(emp.id, "accrual", "1000", run.id))
        account = DepositAccount(
            id=uuid.uuid4(),
            employee_id=emp.id,
            balance=Decimal("3000"),  # расходится с леджером (1000)
            initial_balance=Decimal("0"),
            last_updated=NOW,
        )
        session.add(account)
        await session.commit()

        drift = await find_deposit_balance_drift(session)
        mine = [d for d in drift if d.employee_id == emp.id]
        assert len(mine) == 1
        assert mine[0].balance == Decimal("3000")
        assert mine[0].expected == Decimal("1000")
        assert mine[0].diff == Decimal("2000")

        account.balance = Decimal("1000")
        await session.commit()
        drift = await find_deposit_balance_drift(session)
        assert not [d for d in drift if d.employee_id == emp.id]


@pytest.mark.asyncio
async def test_has_imported_deposit_accruals(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        emp = _employee()
        session.add(emp)
        await session.flush()
        # Обычный (не легаси) accrual — гард молчит.
        normal = await _run(session, status="finalized", is_legacy=False, week=0)
        session.add(_tx(emp.id, "accrual", "1000", normal.id))
        await session.commit()
        assert await deposit_service.has_imported_deposit_accruals(session, emp.id) is False

        # Легаси-импортный accrual — гард срабатывает.
        legacy = await _run(session, status="finalized", is_legacy=True, week=1)
        session.add(_tx(emp.id, "accrual", "2000", legacy.id))
        await session.commit()
        assert await deposit_service.has_imported_deposit_accruals(session, emp.id) is True

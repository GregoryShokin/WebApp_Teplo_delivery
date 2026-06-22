"""Этап 4: сервис отложенной выдачи депозита (deposit_payout_schedule).

Проверяем feature-флаг, создание/замену/отмену pending-плана и переход
pending → processed (финализация) → pending (откат).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    AppSetting,
    DepositPayoutSchedule,
    Employee,
    PayrollPeriod,
    PayrollRun,
)
from app.services.deposit_schedule import (
    DEPOSIT_SCHEDULED_PAYOUT_ENABLED_KEY,
    cancel_pending_schedule,
    create_or_replace_schedule,
    is_scheduled_payout_enabled,
    load_pending_schedules,
    mark_schedules_processed_for_run,
    revert_schedules_for_run,
)


async def _seed_employee(session: AsyncSession) -> Employee:
    employee = Employee(
        id=uuid.uuid4(),
        full_name=f"Повар {uuid.uuid4().hex[:6]}",
        iiko_id=f"iiko-{uuid.uuid4()}",
        category="category_2",
        status="active",
        is_senior=False,
        is_deputy_senior=False,
        pin_hash="hashed-pin",
        pin_set_at=datetime(2026, 5, 1, tzinfo=UTC),
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
        updated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    session.add(employee)
    await session.flush()
    return employee


async def _enable_flag(session: AsyncSession) -> None:
    session.add(
        AppSetting(
            id=uuid.uuid4(),
            key=DEPOSIT_SCHEDULED_PAYOUT_ENABLED_KEY,
            value=True,
            value_type="bool",
            category="payroll",
            display_name="Отложенная выдача депозита",
            widget_type="switch",
        )
    )
    await session.flush()


async def _seed_run(session: AsyncSession) -> PayrollRun:
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="half_month",
        start_date=date(2026, 6, 16),
        end_date=date(2026, 6, 22),
        payroll_date=date(2026, 6, 23),
        status="finalized",
        finalized_at=datetime(2026, 6, 23, tzinfo=UTC),
    )
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=period.id,
        started_at=datetime(2026, 6, 23, tzinfo=UTC),
        finished_at=datetime(2026, 6, 23, 1, tzinfo=UTC),
        status="finalized",
        blocking_issues=[],
        summary={},
        is_imported_legacy=False,
    )
    session.add_all([period, run])
    await session.flush()
    return run


async def test_flag_disabled_by_default_and_enabled_by_setting(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        assert await is_scheduled_payout_enabled(session) is False
        await _enable_flag(session)
        assert await is_scheduled_payout_enabled(session) is True


async def test_create_replace_and_cancel_schedule(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _seed_employee(session)

        first = await create_or_replace_schedule(
            session,
            employee.id,
            requested_amount=None,
            account_choice="safe",
            created_by_user_id=None,
        )
        assert first.status == "pending"
        assert first.requested_amount is None

        # Повторный вызов обновляет тот же pending-план (без дублей).
        second = await create_or_replace_schedule(
            session,
            employee.id,
            requested_amount=Decimal("5000"),
            account_choice="bank_draft",
            created_by_user_id=None,
        )
        assert second.id == first.id
        assert second.requested_amount == Decimal("5000")
        assert second.account_choice == "bank_draft"

        pending = await load_pending_schedules(session, [employee.id])
        assert set(pending) == {employee.id}

        assert await cancel_pending_schedule(session, employee.id) is True
        assert await cancel_pending_schedule(session, employee.id) is False
        assert await load_pending_schedules(session, [employee.id]) == {}


async def test_processed_and_reverted_lifecycle(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _seed_employee(session)
        run = await _seed_run(session)
        await create_or_replace_schedule(
            session,
            employee.id,
            requested_amount=None,
            account_choice="safe",
            created_by_user_id=None,
        )

        # Финализация: pending → processed с привязкой к прогону.
        processed = await mark_schedules_processed_for_run(session, run.id, [employee.id])
        assert processed == 1
        schedule = await session.scalar(
            select(DepositPayoutSchedule).where(
                DepositPayoutSchedule.employee_id == employee.id
            )
        )
        assert schedule.status == "processed"
        assert schedule.target_run_id == run.id
        assert await load_pending_schedules(session, [employee.id]) == {}

        # Откат: processed → pending, target_run_id сброшен.
        reverted = await revert_schedules_for_run(session, run.id)
        assert reverted == 1
        await session.refresh(schedule)
        assert schedule.status == "pending"
        assert schedule.target_run_id is None

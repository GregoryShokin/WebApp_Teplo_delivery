"""Разделение контуров ЗП: производственный (неделя) и административный (полумесяц).

Вкладка «Расчёты» показывает только недельные ведомости, вкладка «Администрация» —
только полумесячные, и кнопка «Пересчитать» каждой вкладки уводит расчёт в свой
движок. До разделения `list_runs` отдавала оба типа вперемешку, а построчный
«Пересчитать» из «Расчётов» отправлял админскую ведомость в `run_payroll` —
производственный расчёт сносил строки прогона и пересобирал их из явок, затирая
оклады управляющих.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    Employee,
    EmployeePositionAssignment,
    PayrollLine,
    PayrollPeriod,
    PayrollRate,
    PayrollRun,
)
from app.services.payroll_admin import run_admin_payroll
from app.services.payroll_runner import (
    PayrollConflictError,
    auto_create_next_period,
    list_runs,
    run_payroll,
)

ADMIN_PERIOD = (date(2026, 5, 1), date(2026, 5, 15), date(2026, 5, 15))
WEEK_PERIOD = (date(2026, 5, 5), date(2026, 5, 11), date(2026, 5, 12))


async def _make_period(
    session: AsyncSession,
    *,
    period_type: str,
    bounds: tuple[date, date, date],
) -> PayrollPeriod:
    start, end, payday = bounds
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type=period_type,
        start_date=start,
        end_date=end,
        payroll_date=payday,
        status="open",
    )
    session.add(period)
    await session.flush()
    return period


async def _make_run(session: AsyncSession, period: PayrollPeriod) -> PayrollRun:
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=period.id,
        started_at=datetime(2026, 5, 12, 9, tzinfo=UTC),
        status="completed",
        blocking_issues=[],
        summary={},
    )
    session.add(run)
    await session.flush()
    return run


async def _make_admin_employee_with_oklad(
    session: AsyncSession,
    *,
    position: str = "Управляющий",
    amount: Decimal = Decimal("60000"),
) -> Employee:
    employee = Employee(
        id=uuid.uuid4(),
        full_name="Админ Сотрудник",
        iiko_id=f"iiko-{uuid.uuid4()}",
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
    session.add(
        EmployeePositionAssignment(
            id=uuid.uuid4(),
            employee_id=employee.id,
            position=position,
            effective_from=date(2026, 1, 1),
            effective_to=None,
        )
    )
    session.add(
        PayrollRate(
            id=uuid.uuid4(),
            employee_id=None,
            position_group=position,
            category="admin",
            station=None,
            rate_type="monthly",
            amount=amount,
            is_active=True,
            effective_from=date(2026, 1, 1),
        )
    )
    await session.flush()
    return employee


async def test_runs_list_shows_only_weekly_production_runs(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Список вкладки «Расчёты» — без ведомостей администрации."""
    async with async_session_factory() as session:
        week_period = await _make_period(session, period_type="week", bounds=WEEK_PERIOD)
        admin_period = await _make_period(session, period_type="half_month", bounds=ADMIN_PERIOD)
        week_run = await _make_run(session, week_period)
        admin_run = await _make_run(session, admin_period)
        await session.commit()

        runs = await list_runs(session)
        listed_ids = {item["id"] for item in runs}
        assert week_run.id in listed_ids
        assert admin_run.id not in listed_ids


async def test_production_recalculation_rejects_admin_period(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Пересчитать» производственного контура не трогает полумесячную ведомость.

    Раньше `run_payroll` на админском периоде удаляла строки прогона и пересобирала их
    из явок: оклады администрации подменялись производственным расчётом.
    """
    async with async_session_factory() as session:
        employee = await _make_admin_employee_with_oklad(session)
        employee_id = employee.id
        period = await _make_period(session, period_type="half_month", bounds=ADMIN_PERIOD)
        period_id = period.id
        await session.commit()

        admin_run = await run_admin_payroll(session, period_id)
        run_id = admin_run.id
        lines_before = {
            (line.employee_id, line.role): line.total_payable
            for line in (
                await session.scalars(select(PayrollLine).where(PayrollLine.run_id == run_id))
            ).all()
        }
        assert list(lines_before) == [(employee_id, "Управляющий")]

        with pytest.raises(PayrollConflictError):
            await run_payroll(session, period_id)

        lines_after = {
            (line.employee_id, line.role): line.total_payable
            for line in (
                await session.scalars(select(PayrollLine).where(PayrollLine.run_id == run_id))
            ).all()
        }
        assert lines_after == lines_before


async def test_auto_create_next_period_appends_after_week_not_admin_period(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Следующая неделя отсчитывается от последней НЕДЕЛИ, а не от админского полумесяца.

    Полумесячный период уходит по датам дальше недельного (напр. 16–31 мая против
    5–11 мая), и без фильтра по типу кнопка «Запустить расчёт» создавала неделю
    с 1 июня, перепрыгнув три недели производственного контура.
    """
    async with async_session_factory() as session:
        week_period = await _make_period(session, period_type="week", bounds=WEEK_PERIOD)
        # Прогон на неделе, чтобы не сработала ветка «пред-созданная неделя без прогона».
        await _make_run(session, week_period)
        await _make_period(
            session,
            period_type="half_month",
            bounds=(date(2026, 5, 16), date(2026, 5, 31), date(2026, 6, 1)),
        )
        await session.commit()

        created = await auto_create_next_period(session)
        assert created.period_type == "week"
        assert created.start_date == date(2026, 5, 12)
        assert created.end_date == date(2026, 5, 18)
        assert created.payroll_date == date(2026, 5, 19)

"""Заливка выручки iiko в базу налога: идемпотентность, журнал правок, вытеснение гранулярностей.

Самое хрупкое место — не арифметика (её проверяет test_taxes_engine), а то, что повторная
заливка не плодит дубли, правки логируются, а дневные и месячные данные одного месяца
не складываются.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.tax import IikoRevenuePeriod, IikoRevenueRevision
from app.services.taxes.repository import DEFAULT_DEPARTMENT, sum_revenue
from app.services.taxes.revenue_sync import (
    RevenueRecord,
    records_from_iiko_monthly,
    sync_revenue_periods,
)

DEP = DEFAULT_DEPARTMENT


def _month(year: int, month: int, net: str, gross: str | None = None) -> RevenueRecord:
    from calendar import monthrange

    return RevenueRecord(
        period_start=date(year, month, 1),
        period_end=date(year, month, monthrange(year, month)[1]),
        granularity="month",
        department=DEP,
        revenue_net=Decimal(net),
        revenue_gross=Decimal(gross) if gross else None,
    )


def _day(year: int, month: int, day: int, net: str) -> RevenueRecord:
    d = date(year, month, day)
    return RevenueRecord(
        period_start=d, period_end=d, granularity="day", department=DEP,
        revenue_net=Decimal(net),
    )


async def _count(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(IikoRevenuePeriod))


async def test_first_load_inserts_all(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        res = await sync_revenue_periods(
            session,
            [_month(2026, 1, "4095915.11"), _month(2026, 2, "3833597.82")],
        )
        await session.commit()

    assert res.inserted == 2
    assert res.updated == 0
    async with async_session_factory() as session:
        assert await _count(session) == 2


async def test_reload_same_data_is_noop(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Повторная заливка тех же чисел не создаёт дублей и не пишет в журнал."""
    async with async_session_factory() as session:
        await sync_revenue_periods(session, [_month(2026, 3, "4006534.78")])
        await session.commit()

    async with async_session_factory() as session:
        res = await sync_revenue_periods(session, [_month(2026, 3, "4006534.78")])
        await session.commit()

    assert res.inserted == 0
    assert res.unchanged == 1
    assert res.revisions == 0
    async with async_session_factory() as session:
        assert await _count(session) == 1
        assert await session.scalar(
            select(func.count()).select_from(IikoRevenueRevision)
        ) == 0


async def test_backdated_change_logs_revision(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """iiko поправил закрытый месяц → строка обновляется, отличие пишется в журнал."""
    async with async_session_factory() as session:
        await sync_revenue_periods(session, [_month(2026, 3, "4006534.78")])
        await session.commit()

    async with async_session_factory() as session:
        res = await sync_revenue_periods(
            session, [_month(2026, 3, "4018934.78")], sync_reason="daily_job"
        )
        await session.commit()

    assert res.updated == 1
    assert res.revisions == 1
    async with async_session_factory() as session:
        rev = (
            await session.execute(select(IikoRevenueRevision))
        ).scalar_one()
        assert rev.revenue_net_old == Decimal("4006534.78")
        assert rev.revenue_net_new == Decimal("4018934.78")
        assert rev.sync_reason == "daily_job"


async def test_daily_supersedes_month_no_double_count(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Подневная заливка вытесняет месячную строку того же месяца — сумма не задваивается."""
    async with async_session_factory() as session:
        await sync_revenue_periods(session, [_month(2026, 1, "4095915.11")])
        await session.commit()

    async with async_session_factory() as session:
        res = await sync_revenue_periods(
            session,
            [_day(2026, 1, 1, "150000.00"), _day(2026, 1, 2, "160000.00")],
        )
        await session.commit()
        assert res.superseded_rows == 1

    async with async_session_factory() as session:
        total, covered = await sum_revenue(
            session, start=date(2026, 1, 1), end=date(2026, 1, 31)
        )
        # Только два дня, месячная строка ушла — двойного счёта нет.
        assert total == Decimal("310000.00")
        assert covered == {(2026, 1)}


async def test_month_supersedes_daily(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Обратно: месячная строка вытесняет ранее залитые дни этого месяца."""
    async with async_session_factory() as session:
        await sync_revenue_periods(
            session, [_day(2026, 2, 1, "100000.00"), _day(2026, 2, 2, "120000.00")]
        )
        await session.commit()

    async with async_session_factory() as session:
        res = await sync_revenue_periods(session, [_month(2026, 2, "3833597.82")])
        await session.commit()
        assert res.superseded_rows == 2

    async with async_session_factory() as session:
        total, _ = await sum_revenue(
            session, start=date(2026, 2, 1), end=date(2026, 2, 28)
        )
        assert total == Decimal("3833597.82")


async def test_dry_run_writes_nothing(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        res = await sync_revenue_periods(
            session, [_month(2026, 1, "4095915.11")], apply=False
        )
        await session.commit()

    assert res.inserted == 1  # посчитано
    async with async_session_factory() as session:
        assert await _count(session) == 0  # но не записано


def test_records_from_iiko_monthly_sums_directions() -> None:
    """Строки по направлениям сворачиваются в одну месячную запись."""
    rows = [
        {"DishDiscountSumInt": 63444.54, "DishSumInt": 75581, "DiscountSum": 12136.46},
        {"DishDiscountSumInt": 4032471.25, "DishSumInt": 4613472, "DiscountSum": 581000.65},
    ]
    rec = records_from_iiko_monthly(rows, year=2026, month=1, department=DEP)

    assert rec.granularity == "month"
    assert rec.period_start == date(2026, 1, 1)
    assert rec.period_end == date(2026, 1, 31)
    assert rec.revenue_net == Decimal("63444.54") + Decimal("4032471.25")

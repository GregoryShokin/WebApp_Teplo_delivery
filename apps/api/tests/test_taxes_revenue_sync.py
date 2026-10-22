"""Заливка выручки iiko в базу налога: идемпотентность, журнал правок, вытеснение гранулярностей.

Самое хрупкое место — не арифметика (её проверяет test_taxes_engine), а то, что повторная
заливка не плодит дубли, правки логируются, а дневные и месячные данные одного месяца
не складываются.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.tax import IikoRevenuePeriod, IikoRevenueRevision
from app.services.taxes.repository import DEFAULT_DEPARTMENT, sum_revenue
from app.services.taxes.revenue_sync import (
    RevenueRecord,
    RevenueSyncError,
    records_from_iiko_monthly,
    revenue_sync_window,
    sync_revenue_from_iiko,
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


# ── Подкачка из iiko: окно, вытеснение, отказ на пустом ответе ──────────────────


def _fake_fetch(by_day: dict[date, str], calls: list[tuple[date, date]] | None = None):
    async def fetch(_session, date_from, date_to):
        if calls is not None:
            calls.append((date_from, date_to))
        return {d: Decimal(v) for d, v in by_day.items()}

    return fetch


async def test_iiko_sync_writes_days_from_month_start(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Окно расширяется до 1-го числа: иначе дни вытеснили бы месячную строку, а пол-месяца
    выручки пропало бы из базы."""
    calls: list[tuple[date, date]] = []
    async with async_session_factory() as session:
        await sync_revenue_periods(session, [_month(2026, 7, "3596783.28")])
        report = await sync_revenue_from_iiko(
            session,
            date_from=date(2026, 7, 15),
            date_to=date(2026, 8, 2),
            fetch=_fake_fetch(
                {
                    date(2026, 7, 1): "100000.00",
                    date(2026, 7, 31): "120000.50",
                    date(2026, 8, 1): "90000.00",
                    date(2026, 8, 2): "95000.00",
                },
                calls,
            ),
        )
        await session.commit()

    assert calls == [(date(2026, 7, 1), date(2026, 8, 2))]
    assert report.days == 4
    assert report.months == {"2026-07": Decimal("220000.50"), "2026-08": Decimal("185000.00")}
    assert report.result.superseded_rows == 1  # месячная строка июля ушла
    async with async_session_factory() as session:
        total, covered = await sum_revenue(session, start=date(2026, 7, 1), end=date(2026, 8, 2))
    assert total == Decimal("405000.50")
    assert covered == {(2026, 7), (2026, 8)}


async def test_iiko_sync_refuses_empty_report(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Пустой ответ iiko — сбой, а не «выручки не было»: базу не трогаем."""
    async with async_session_factory() as session:
        await sync_revenue_periods(session, [_month(2026, 7, "3596783.28")])
        await session.commit()

    async with async_session_factory() as session:
        with pytest.raises(RevenueSyncError, match="пустой отчёт"):
            await sync_revenue_from_iiko(
                session,
                date_from=date(2026, 7, 1),
                date_to=date(2026, 7, 31),
                fetch=_fake_fetch({}),
            )
        await session.rollback()

    async with async_session_factory() as session:
        total, _ = await sum_revenue(session, start=date(2026, 7, 1), end=date(2026, 7, 31))
    assert total == Decimal("3596783.28")


async def test_iiko_sync_turns_system_exit_into_error(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """IikoClient без адреса сервера бросает SystemExit — он не должен ронять процесс."""
    async def broken(_session, _date_from, _date_to):
        raise SystemExit("IIKO_SERVER_BASE_URL is not set")

    async with async_session_factory() as session:
        with pytest.raises(RevenueSyncError, match="не настроен"):
            await sync_revenue_from_iiko(
                session, date_from=date(2026, 7, 1), date_to=date(2026, 7, 31), fetch=broken
            )


async def test_iiko_sync_zeroes_day_that_vanished_from_iiko(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """День был в базе, а в новом отчёте iiko его нет (заказы удалены) — обнуляем с журналом."""
    async with async_session_factory() as session:
        await sync_revenue_from_iiko(
            session,
            date_from=date(2026, 9, 1),
            date_to=date(2026, 9, 2),
            fetch=_fake_fetch({date(2026, 9, 1): "100", date(2026, 9, 2): "200"}),
        )
        await session.commit()

    async with async_session_factory() as session:
        report = await sync_revenue_from_iiko(
            session,
            date_from=date(2026, 9, 1),
            date_to=date(2026, 9, 2),
            fetch=_fake_fetch({date(2026, 9, 1): "100"}),
            sync_reason="manual_sync",
        )
        await session.commit()

    assert report.result.revisions == 1
    async with async_session_factory() as session:
        total, _ = await sum_revenue(session, start=date(2026, 9, 1), end=date(2026, 9, 30))
        revision = (await session.scalars(select(IikoRevenueRevision))).one()
    assert total == Decimal("100.00")
    assert revision.revenue_net_new == Decimal("0.00")
    assert revision.sync_reason == "manual_sync"


async def test_sync_window_reaches_back_to_first_month_without_revenue(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Октябрь 2026: выручка залита только по июнь — ночной прогон догружает с 1 июля.

    Ровно эта дыра дала УСН за III квартал = 0: июль–сентябрь никто не залил руками.
    """
    async with async_session_factory() as session:
        await sync_revenue_periods(session, [_month(2026, m, "1000") for m in range(1, 7)])
        await session.commit()
        window = await revenue_sync_window(session, today=date(2026, 10, 22))
    assert window == (date(2026, 7, 1), date(2026, 10, 21))


async def test_sync_window_rereads_previous_month_when_nothing_is_missing(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Все месяцы на месте — окно с начала прошлого месяца: iiko правит закрытые дни."""
    async with async_session_factory() as session:
        await sync_revenue_periods(session, [_month(2026, m, "1000") for m in range(1, 11)])
        await session.commit()
        window = await revenue_sync_window(session, today=date(2026, 11, 5))
        january = await revenue_sync_window(session, today=date(2027, 1, 1))
    assert window == (date(2026, 10, 1), date(2026, 11, 4))
    # 1 января: вчера — 31 декабря, ноябрь и декабрь без выручки — тянем с ноября.
    assert january == (date(2026, 11, 1), date(2026, 12, 31))

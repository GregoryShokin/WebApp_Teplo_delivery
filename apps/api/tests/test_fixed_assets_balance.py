"""Строки баланса и строка ОПиУ по основным средствам: заморозка и детектор дрейфа.

Балансового модуля в приложении нет — цифры переносят в таблицы руками. Здесь проверяется,
что переносить есть что и что перенесённое не поедет незаметно.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AssetBalanceSnapshot, AssetCategory, FixedAsset
from app.services.asset_balance import (
    NOT_WORKING_LINE,
    balance_lines,
    compare_with_snapshot,
    depreciation_series,
    snapshot_month,
)
from app.services.fixed_assets import accrue_depreciation, close_month, correct_depreciation


async def _category(session: AsyncSession, name: str, life: int = 84) -> AssetCategory:
    """Категория по имени: десять штук уже засеяны миграцией 0221, имя уникально."""
    category = await session.scalar(select(AssetCategory).where(AssetCategory.name == name))
    if category is None:
        category = AssetCategory(name=name, useful_life_months=life)
        session.add(category)
    else:
        category.useful_life_months = life
    await session.flush()
    return category


async def _asset(
    session: AsyncSession,
    *,
    category: AssetCategory,
    cost: str,
    status: str = "in_use",
) -> FixedAsset:
    asset = FixedAsset(
        name=f"Объект {cost}",
        initial_cost=Decimal(cost),
        category_id=category.id,
        commissioned_on=date(2026, 1, 1),
        status=status,
        valuation_basis="market",
    )
    session.add(asset)
    await session.flush()
    return asset


async def test_not_working_gets_its_own_line_and_leaves_the_category(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Строк одиннадцать при десяти категориях — и объект не должен попасть в обе.

    «Не работающее оборудование» — статус карточки, а не категория. Витрина по стоимости лома
    обязана уйти в свою строку и ИСЧЕЗНУТЬ из строки «Холодильное»: иначе она посчиталась бы
    дважды и сумма строк перестала бы сходиться с реестром.
    """
    async with async_session_factory() as session:
        cold = await _category(session, "Холодильное/морозильное оборудование")
        await _asset(session, category=cold, cost="100000.00")
        await _asset(session, category=cold, cost="4000.00", status="not_working")
        await session.commit()

        lines = {
            line.line_name: line for line in await balance_lines(session, as_of=date(2026, 1, 1))
        }

        assert lines["Холодильное/морозильное оборудование"].asset_count == 1
        assert lines["Холодильное/морозильное оборудование"].initial_cost == Decimal("100000.00")
        assert lines[NOT_WORKING_LINE].asset_count == 1
        assert lines[NOT_WORKING_LINE].initial_cost == Decimal("4000.00")
        # Сумма строк равна реестру — двойного счёта нет.
        assert sum(line.initial_cost for line in lines.values()) == Decimal("104000.00")


async def test_residual_is_taken_as_of_the_month_not_as_of_today(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Отчёт за январь не должен поехать от того, что февраль уже закрыт.

    Хранимый ``residual_after`` для этого не годится: его переписывает пересчёт при любой
    правке прошлого. Остаток выводится заново — начисления ВКЛЮЧИТЕЛЬНО по нужный месяц.
    """
    async with async_session_factory() as session:
        category = await _category(session, "Тепловое оборудование", life=100)
        await _asset(session, category=category, cost="100000.00")
        await session.commit()

        await accrue_depreciation(session, period_month=date(2026, 1, 1))
        await accrue_depreciation(session, period_month=date(2026, 2, 1))
        await session.commit()

        january = (await balance_lines(session, as_of=date(2026, 1, 1)))[0]
        february = (await balance_lines(session, as_of=date(2026, 2, 1)))[0]

        assert january.residual == Decimal("99000.00")
        assert january.depreciation == Decimal("1000.00")
        assert february.residual == Decimal("98000.00")


async def test_closing_the_month_freezes_the_lines(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Снимок снимается там, где цифра стала окончательной, — в закрытии месяца."""
    async with async_session_factory() as session:
        category = await _category(session, "Тепловое оборудование", life=100)
        await _asset(session, category=category, cost="100000.00")
        await session.commit()

        run = await close_month(session, period_month=date(2026, 1, 1))
        assert run.result["balance_lines"] == 1

        frozen = (await session.scalars(select(AssetBalanceSnapshot))).all()
        assert len(frozen) == 1
        assert Decimal(str(frozen[0].residual)) == Decimal("99000.00")
        assert Decimal(str(frozen[0].depreciation)) == Decimal("1000.00")


async def test_drift_is_caught_when_the_past_is_moved(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Прошлое сдвинули после заморозки — владелец должен узнать, а не найти при сверке.

    Правку двигают три пути: ручная коррекция, применённая переоценка и правка карточки. Все
    законны, но отчётность за закрытый месяц уже ушла — расхождение обязано всплыть само.
    """
    async with async_session_factory() as session:
        category = await _category(session, "Тепловое оборудование", life=100)
        asset = await _asset(session, category=category, cost="100000.00")
        await close_month(session, period_month=date(2026, 1, 1))

        assert await compare_with_snapshot(session, period_month=date(2026, 1, 1)) == []

        await correct_depreciation(
            session,
            asset_id=asset.id,
            period_month=date(2026, 1, 1),
            amount=Decimal("400.00"),
            note="Объект введён позже",
        )
        await session.commit()

        drift = await compare_with_snapshot(session, period_month=date(2026, 1, 1))
        fields = {item.field for item in drift}
        assert "остаточная" in fields
        assert "амортизация за месяц" in fields
        moved = next(item for item in drift if item.field == "остаточная")
        assert moved.snapshot_value == Decimal("99000.00")
        assert moved.current_value == Decimal("99600.00")


async def test_disposed_assets_leave_the_balance(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Списанный объект ушёл из внеоборотных активов — в строках его быть не должно."""
    async with async_session_factory() as session:
        category = await _category(session, "Тепловое оборудование")
        await _asset(session, category=category, cost="100000.00")
        await _asset(session, category=category, cost="50000.00", status="sold")
        await session.commit()

        lines = await balance_lines(session, as_of=date(2026, 1, 1))
        assert len(lines) == 1
        assert lines[0].initial_cost == Decimal("100000.00")


async def test_series_gives_the_pnl_row_by_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Помесячный ряд — это и есть строка «УчОС Амортизация», которую переносят в ОПиУ."""
    async with async_session_factory() as session:
        category = await _category(session, "Тепловое оборудование", life=100)
        await _asset(session, category=category, cost="100000.00")
        await session.commit()

        for month in (date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)):
            await accrue_depreciation(session, period_month=month)
        await session.commit()

        series = await depreciation_series(session)
        assert series == [
            (date(2026, 1, 1), Decimal("1000.00")),
            (date(2026, 2, 1), Decimal("1000.00")),
            (date(2026, 3, 1), Decimal("1000.00")),
        ]

        narrowed = await depreciation_series(
            session, date_from=date(2026, 2, 1), date_to=date(2026, 2, 28)
        )
        assert narrowed == [(date(2026, 2, 1), Decimal("1000.00"))]


async def test_snapshot_drops_a_line_that_no_longer_exists(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Последний объект категории списан — строка не должна остаться в снимке призраком."""
    async with async_session_factory() as session:
        heat = await _category(session, "Тепловое оборудование")
        cold = await _category(session, "Холодильное/морозильное оборудование")
        await _asset(session, category=heat, cost="100000.00")
        doomed = await _asset(session, category=cold, cost="50000.00")
        await session.commit()

        await snapshot_month(session, period_month=date(2026, 1, 1))
        await session.commit()
        assert len((await session.scalars(select(AssetBalanceSnapshot))).all()) == 2

        doomed.status = "disposed"
        await snapshot_month(session, period_month=date(2026, 1, 1))
        await session.commit()

        rows = (await session.scalars(select(AssetBalanceSnapshot))).all()
        assert [row.line_name for row in rows] == ["Тепловое оборудование"]

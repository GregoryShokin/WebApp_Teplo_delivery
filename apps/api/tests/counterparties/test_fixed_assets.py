"""Учёт основных средств: линейная помесячная амортизация и правило «ремонт vs модернизация».

Методология владельца: единый линейный метод, СПИ из категории с переопределением в карточке,
старт с месяца ввода в эксплуатацию, порог модернизации 15% от первоначальной стоимости.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AssetCategory, DepreciationEntry, FixedAsset
from app.services.fixed_assets import (
    accrue_depreciation,
    capitalize_upgrade,
    classify_asset_expense,
    residual_value,
)


async def _asset(
    session: AsyncSession,
    *,
    cost: str,
    life: int | None = None,
    category_life: int | None = None,
    commissioned_on: date | None = date(2026, 1, 10),
    status: str = "in_use",
) -> FixedAsset:
    category_id = None
    if category_life is not None:
        category = AssetCategory(
            name=f"Категория-{cost}-{category_life}", useful_life_months=category_life
        )
        session.add(category)
        await session.flush()
        category_id = category.id
    asset = FixedAsset(
        name=f"ОС-{cost}",
        initial_cost=Decimal(cost),
        useful_life_months=life,
        category_id=category_id,
        commissioned_on=commissioned_on,
        status=status,
        valuation_basis="market",
    )
    session.add(asset)
    await session.flush()
    return asset


async def test_monthly_depreciation_is_linear(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """60 000 при СПИ 60 месяцев → ровно 1 000 в месяц, остаток уменьшается на ту же сумму."""
    async with async_session_factory() as session:
        asset = await _asset(session, cost="60000.00", life=60)
        await session.commit()

        first = await accrue_depreciation(session, period_month=date(2026, 1, 1))
        await session.commit()
        assert len(first) == 1
        assert first[0].amount == Decimal("1000.00")
        assert first[0].residual_after == Decimal("59000.00")

        await accrue_depreciation(session, period_month=date(2026, 2, 1))
        await session.commit()
        assert await residual_value(session, asset) == Decimal("58000.00")


async def test_accrual_is_idempotent_for_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Повторное закрытие месяца не задваивает начисление — на этом держится пересчёт."""
    async with async_session_factory() as session:
        asset = await _asset(session, cost="12000.00", life=12)
        await session.commit()

        await accrue_depreciation(session, period_month=date(2026, 1, 1), asset_id=asset.id)
        await session.commit()
        again = await accrue_depreciation(session, period_month=date(2026, 1, 1), asset_id=asset.id)
        await session.commit()

        assert again == []
        count = await session.scalar(
            select(func.count(DepreciationEntry.id)).where(DepreciationEntry.asset_id == asset.id)
        )
        assert count == 1


async def test_not_commissioned_asset_is_not_depreciated(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Купленное в резерв не амортизируется, пока не введено в эксплуатацию."""
    async with async_session_factory() as session:
        reserve = await _asset(session, cost="30000.00", life=30, commissioned_on=None)
        future = await _asset(session, cost="30000.00", life=30, commissioned_on=date(2026, 5, 1))
        await session.commit()

        await accrue_depreciation(session, period_month=date(2026, 1, 1), asset_id=reserve.id)
        await accrue_depreciation(session, period_month=date(2026, 1, 1), asset_id=future.id)
        await session.commit()

        assert await residual_value(session, reserve) == Decimal("30000.00")
        assert await residual_value(session, future) == Decimal("30000.00")


async def test_useful_life_falls_back_to_category(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """СПИ берётся из категории, если в карточке не задан."""
    async with async_session_factory() as session:
        asset = await _asset(session, cost="24000.00", life=None, category_life=24)
        await session.commit()

        entries = await accrue_depreciation(
            session, period_month=date(2026, 1, 1), asset_id=asset.id
        )
        await session.commit()
        assert entries[0].amount == Decimal("1000.00")


async def test_last_month_closes_residual_without_kopeck_tail(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Округление не оставляет вечного хвоста: 1 000 / 3 мес = 333,33 + 333,33 + 333,34.

    Без дотягивания остатка в последнем месяце объект никогда не самортизировался бы полностью,
    и в балансе висела бы копейка внеоборотных активов.
    """
    async with async_session_factory() as session:
        asset = await _asset(session, cost="1000.00", life=3)
        await session.commit()

        for month in (date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)):
            await accrue_depreciation(session, period_month=month, asset_id=asset.id)
            await session.commit()

        assert await residual_value(session, asset) == Decimal("0.00")
        amounts = (
            await session.scalars(
                select(DepreciationEntry.amount)
                .where(DepreciationEntry.asset_id == asset.id)
                .order_by(DepreciationEntry.period_month)
            )
        ).all()
        assert [Decimal(str(a)) for a in amounts] == [
            Decimal("333.33"),
            Decimal("333.33"),
            Decimal("333.34"),
        ]


async def test_fully_depreciated_asset_stops_accruing(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """После полной амортизации начисления прекращаются — в минус не уходим."""
    async with async_session_factory() as session:
        asset = await _asset(session, cost="2000.00", life=2)
        await session.commit()

        for month in (date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)):
            await accrue_depreciation(session, period_month=month, asset_id=asset.id)
            await session.commit()

        assert await residual_value(session, asset) == Decimal("0.00")
        count = await session.scalar(
            select(func.count(DepreciationEntry.id)).where(DepreciationEntry.asset_id == asset.id)
        )
        assert count == 2


async def test_sold_asset_is_not_depreciated(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Проданный объект выбыл из учёта — амортизация по нему не идёт."""
    async with async_session_factory() as session:
        asset = await _asset(session, cost="50000.00", life=50, status="sold")
        await session.commit()

        await accrue_depreciation(session, period_month=date(2026, 1, 1))
        await session.commit()
        assert await residual_value(session, asset) == Decimal("50000.00")


def test_repair_vs_upgrade_threshold() -> None:
    """Правило владельца: <15% — ремонт, >15% — модернизация, ровно 15% — решает владелец."""
    base = Decimal("100000.00")
    assert classify_asset_expense(base, Decimal("14999.00")) == "repair"
    assert classify_asset_expense(base, Decimal("15000.00")) == "requires_owner_review"
    assert classify_asset_expense(base, Decimal("15001.00")) == "upgrade"
    # Нулевая база (legacy без оценки) сама по себе требует разбора.
    assert classify_asset_expense(Decimal("0"), Decimal("100.00")) == "requires_owner_review"


async def test_upgrade_capitalizes_and_raises_future_depreciation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Модернизация увеличивает базу: следующий месяц амортизируется уже по новой стоимости."""
    async with async_session_factory() as session:
        asset = await _asset(session, cost="120000.00", life=120)
        await session.commit()

        await accrue_depreciation(session, period_month=date(2026, 1, 1), asset_id=asset.id)
        await session.commit()
        assert await residual_value(session, asset) == Decimal("119000.00")

        await capitalize_upgrade(session, asset=asset, amount=Decimal("60000.00"))
        await session.commit()
        # База 180 000 при СПИ 120 → 1 500 в месяц; остаток вырос на сумму модернизации.
        entries = await accrue_depreciation(
            session, period_month=date(2026, 2, 1), asset_id=asset.id
        )
        await session.commit()
        assert entries[0].amount == Decimal("1500.00")
        assert await residual_value(session, asset) == Decimal("177500.00")

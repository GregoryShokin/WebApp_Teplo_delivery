"""Строки баланса и строка ОПиУ по основным средствам: заморозка и детектор дрейфа.

Балансового модуля в приложении нет — цифры переносят в таблицы руками. Здесь проверяется,
что переносить есть что и что перенесённое не поедет незаметно.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AssetBalanceSnapshot, AssetCategory, FixedAsset
from app.services.asset_balance import (
    NOT_WORKING_LINE,
    balance_lines,
    compare_with_snapshot,
    depreciation_series,
    report_lines,
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
    commissioned_on: date | None = date(2026, 1, 1),
    valued_on: date | None = None,
    created_at: datetime | None = None,
) -> FixedAsset:
    asset = FixedAsset(
        name=f"Объект {cost}",
        initial_cost=Decimal(cost),
        category_id=category.id,
        commissioned_on=commissioned_on,
        valued_on=valued_on,
        status=status,
        valuation_basis="market",
    )
    if created_at is not None:
        asset.created_at = created_at
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


async def test_asset_bought_later_does_not_stand_in_an_earlier_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Августовская покупка не должна расти в июльской строке — и растёт она ПОЛНОЙ стоимостью.

    Выбытие в этом расчёте всегда учитывалось по своему месяцу, а появление — нет. Из-за этого
    карточка, заведённая после снятия снимка, вставала во ВСЕ прошлые месяцы, причём без износа
    (начислений за те месяцы у неё нет), то есть по первоначальной стоимости целиком.
    """
    async with async_session_factory() as session:
        category = await _category(session, "Тепловое оборудование", life=100)
        await _asset(session, category=category, cost="100000.00", commissioned_on=date(2026, 7, 1))
        await _asset(session, category=category, cost="30000.00", commissioned_on=date(2026, 8, 5))
        await session.commit()

        july = (await balance_lines(session, as_of=date(2026, 7, 31)))[0]
        august = (await balance_lines(session, as_of=date(2026, 8, 31)))[0]

        assert july.asset_count == 1
        assert july.initial_cost == Decimal("100000.00")
        assert august.asset_count == 2
        assert august.initial_cost == Decimal("130000.00")


async def test_asset_in_reserve_enters_the_balance_by_its_valuation_date(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Купленный в резерв объект — уже актив, хотя амортизация ещё не идёт.

    ``commissioned_on`` у него пуст по методологии («куплен в резерв и не введён»), поэтому
    появление считается по минимуму из непустых дат: по одному только вводу такой объект
    выпал бы из баланса совсем.
    """
    async with async_session_factory() as session:
        category = await _category(session, "Тепловое оборудование", life=100)
        await _asset(
            session,
            category=category,
            cost="60000.00",
            commissioned_on=None,
            valued_on=date(2026, 3, 10),
        )
        await session.commit()

        february = await balance_lines(session, as_of=date(2026, 2, 28))
        march = await balance_lines(session, as_of=date(2026, 3, 31))

        assert february == []
        assert march[0].initial_cost == Decimal("60000.00")
        # Не введён — износа нет, стоит по полной. Это правильно: актив есть, амортизации нет.
        assert march[0].residual == Decimal("60000.00")
        assert march[0].depreciation == Decimal("0.00")


async def test_asset_without_dates_falls_back_to_the_moscow_day_of_its_creation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Обе даты пусты — берём день создания карточки ПО МОСКВЕ, а не по UTC.

    Карточка, заведённая 1 сентября в 00:30 МСК, в UTC ещё 31 августа. По UTC-дате она попала бы
    в августовскую опись, которой не принадлежит: разница в три часа сдвигает месяц.
    """
    async with async_session_factory() as session:
        category = await _category(session, "Тепловое оборудование", life=100)
        await _asset(
            session,
            category=category,
            cost="15000.00",
            commissioned_on=None,
            valued_on=None,
            created_at=datetime(2026, 8, 31, 21, 30, tzinfo=UTC),
        )
        await session.commit()

        assert await balance_lines(session, as_of=date(2026, 8, 31)) == []
        assert (await balance_lines(session, as_of=date(2026, 9, 30)))[0].asset_count == 1


async def test_report_lines_serve_the_frozen_month_not_the_recount(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Закрытый месяц отдаёт снимок: отчёт, уже ушедший владельцу, не должен меняться.

    И расхождение при этом не прячется — оно уходит в ``compare_with_snapshot``.
    """
    async with async_session_factory() as session:
        category = await _category(session, "Тепловое оборудование", life=100)
        asset = await _asset(session, category=category, cost="100000.00")
        await close_month(session, period_month=date(2026, 1, 1))
        await session.commit()

        asset.initial_cost = Decimal("150000.00")
        await session.commit()

        lines, frozen = await report_lines(session, period_month=date(2026, 1, 1))
        assert frozen is True
        assert lines[0].initial_cost == Decimal("100000.00")
        assert lines[0].residual == Decimal("99000.00")

        # Живой расчёт видит правку — на нём и держится детектор дрейфа.
        live = await balance_lines(session, as_of=date(2026, 1, 1))
        assert live[0].initial_cost == Decimal("150000.00")
        drift = await compare_with_snapshot(session, period_month=date(2026, 1, 1))
        assert "первоначальная" in {item.field for item in drift}


async def test_report_lines_compute_live_while_the_month_is_open(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Незакрытый месяц считается вживую и честно помечен незамороженным."""
    async with async_session_factory() as session:
        category = await _category(session, "Тепловое оборудование", life=100)
        await _asset(session, category=category, cost="100000.00")
        await session.commit()

        lines, frozen = await report_lines(session, period_month=date(2026, 1, 1))
        assert frozen is False
        assert lines[0].initial_cost == Decimal("100000.00")


async def test_drift_catches_a_swap_that_keeps_the_residual(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Состав строки подменили, остаточная сошлась — это тоже дрейф.

    Детектор смотрел только на остаточную и амортизацию за месяц. Замена одной карточки на
    другую с той же стоимостью такую проверку проходит молча, а строка баланса при этом стоит
    на других объектах — сверить её с реестром уже нельзя.
    """
    async with async_session_factory() as session:
        category = await _category(session, "Тепловое оборудование", life=100)
        first = await _asset(session, category=category, cost="100000.00")
        await close_month(session, period_month=date(2026, 1, 1))
        await session.commit()

        assert await compare_with_snapshot(session, period_month=date(2026, 1, 1)) == []

        # Ту же стоимость разложили на два объекта: остаточная и износ не изменились.
        first.initial_cost = Decimal("60000.00")
        await _asset(session, category=category, cost="40000.00")
        await session.commit()

        await accrue_depreciation(session, period_month=date(2026, 1, 1))
        await session.commit()

        drift = await compare_with_snapshot(session, period_month=date(2026, 1, 1))
        fields = {item.field for item in drift}
        assert "объектов в строке" in fields

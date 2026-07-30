"""Учёт основных средств: линейная помесячная амортизация и правило «ремонт vs модернизация».

Методология владельца: единый линейный метод, СПИ из категории с переопределением в карточке,
старт с месяца ввода в эксплуатацию, порог модернизации 15% от первоначальной стоимости.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.jobs.depreciation_job import previous_month
from app.models import AppSetting, AssetCategory, DepreciationEntry, FixedAsset
from app.models.fixed_asset import FIXED_ASSET_THRESHOLD
from app.scripts.import_fixed_assets_registry import run
from app.services.fixed_assets import (
    CAPITALIZATION_THRESHOLD_KEY,
    FixedAssetError,
    accrue_depreciation,
    capitalization_threshold,
    capitalize_upgrade,
    classify_asset_expense,
    close_month,
    correct_depreciation,
    create_asset,
    is_fixed_asset_purchase,
    next_inventory_number,
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


async def test_inventory_number_is_generated_in_sequence(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Номер присваивается сам и растёт по серии — на сотрудника нумерацию не вешаем."""
    async with async_session_factory() as session:
        first = await create_asset(session, name="Печь", initial_cost=Decimal("120000.00"))
        second = await create_asset(session, name="Стеллаж", initial_cost=Decimal("15000.00"))
        await session.commit()

        assert first.inventory_number == "ОС-0001"
        assert second.inventory_number == "ОС-0002"


async def test_foreign_numbering_does_not_shift_the_series(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Наклейки со старых описей серию не двигают.

    На холодильниках Черниковой физически наклеены «Холодильный стол № 2» и подобные. Если
    считать максимум по ним, генератор начал бы выдавать номера из чужой нумерации.
    """
    async with async_session_factory() as session:
        await create_asset(
            session,
            name="Стол холодильный",
            initial_cost=Decimal("60000.00"),
            inventory_number="Холодильный стол № 2",
        )
        await session.commit()

        assert await next_inventory_number(session) == "ОС-0001"


async def test_taken_inventory_number_is_reported_not_silently_reused(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Явно заданный занятый номер — ошибка, а не тихий дубль на двух разных предметах."""
    async with async_session_factory() as session:
        await create_asset(
            session, name="Печь", initial_cost=Decimal("120000.00"), inventory_number="ОС-0007"
        )
        await session.commit()

        with pytest.raises(FixedAssetError):
            await create_asset(
                session,
                name="Другая печь",
                initial_cost=Decimal("90000.00"),
                inventory_number="ОС-0007",
            )

        # Внешняя транзакция цела — конфликт заперт в SAVEPOINT. Это и есть смысл вложенной
        # транзакции: заливка реестра на 149 карточек не должна падать целиком из-за одной.
        nxt = await create_asset(session, name="Стеллаж", initial_cost=Decimal("15000.00"))
        await session.commit()
        assert nxt.inventory_number == "ОС-0008"


async def test_recognition_threshold_boundary_is_inclusive(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ровно 10 000 ₽ — уже основное средство (решение владельца 2026-07-30)."""
    async with async_session_factory() as session:
        assert await capitalization_threshold(session) == Decimal("10000.00")
        assert await is_fixed_asset_purchase(session, Decimal("10000.00")) is True
        assert await is_fixed_asset_purchase(session, Decimal("9999.99")) is False
        assert await is_fixed_asset_purchase(session, Decimal("10000.01")) is True


async def test_threshold_follows_owner_setting_not_the_constant(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Владелец меняет порог на странице «Настройки» — расчёт обязан пойти за ним.

    Порог жил в двух местах сразу: константой в модели и настройкой в базе. Пока код читал
    константу, правка в интерфейсе не меняла ничего, и два источника расходились молча.
    """
    async with async_session_factory() as session:
        await session.execute(
            update(AppSetting)
            .where(AppSetting.key == CAPITALIZATION_THRESHOLD_KEY)
            .values(value=50000)
        )
        await session.commit()

        assert await capitalization_threshold(session) == Decimal("50000")
        assert await is_fixed_asset_purchase(session, Decimal("20000.00")) is False


async def test_threshold_falls_back_to_constant_when_setting_is_missing(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Без настройки правило не падает, а берёт умолчание — пустая база не ломает разбор платежа."""
    async with async_session_factory() as session:
        await session.execute(
            delete(AppSetting).where(AppSetting.key == CAPITALIZATION_THRESHOLD_KEY)
        )
        await session.commit()

        assert await capitalization_threshold(session) == FIXED_ASSET_THRESHOLD


async def test_not_working_asset_is_not_depreciated(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Неработающий объект не амортизируется, даже когда категория со сроком у него есть.

    Методология инвентаризации 2026: неработающая техника стоит по стоимости лома и ждёт
    утилизации. Категорию ей ставим настоящую (починят — статус сменится и график поедет по
    своей категории), поэтому останавливать начисление обязан именно статус.
    """
    async with async_session_factory() as session:
        broken = await _asset(session, cost="4000.00", category_life=84, status="not_working")
        working = await _asset(session, cost="84000.00", category_life=84)
        await session.commit()

        entries = await accrue_depreciation(session, period_month=date(2026, 2, 1))
        await session.commit()

        assert {entry.asset_id for entry in entries} == {working.id}
        assert await residual_value(session, broken) == Decimal("4000.00")


async def test_registry_import_expands_quantities_and_is_idempotent(
    async_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Позиция «3 шт» становится тремя карточками, повторный прогон ничего не задваивает.

    Одна карточка = одна физическая единица: инвентарный номер на группу из трёх ларей
    бессмыслен, а списать один из трёх нечем. Строка «ИТОГО» в конце листа не импортируется.
    """
    book = Workbook()
    sheet = book.active
    sheet.title = "Реестр ОС"
    sheet.append(
        [
            "№",
            "Источник",
            "№ описи",
            "Наименование",
            "Бренд / модель",
            "Кол-во",
            "Стоимость за ед., ₽",
            "Стоимость всего, ₽",
            "Тип ОС",
            "Стр. баланса",
            "Дата ввода",
        ]
    )
    cold = "Холодильное/морозильное оборудование"
    heat = "Тепловое оборудование"
    started = datetime(2026, 8, 1)
    rows = [
        [1, "Черникова", 27, "Ларь морозильный", "POLAIR", 3, 30000, 90000, cold, 2, started],
        [2, "Склад", 12, "Печь для пиццы", "ItPizza ML44", 1, 60000, 60000, heat, 1, started],
        # Итоговая строка листа: номера позиции у неё нет, импортировать её нельзя.
        [None, None, None, "ИТОГО", None, 4, None, 150000, None, None, None],
    ]
    for row in rows:
        sheet.append(row)
    path = tmp_path / "Реестр.xlsx"
    book.save(path)

    async with async_session_factory() as session:
        first = await run(session, path, dry_run=False)
        assert first["created"] == 4
        assert first["total"] == Decimal("150000")

        assets = (await session.scalars(select(FixedAsset).order_by(FixedAsset.source_ref))).all()
        refs = [asset.source_ref for asset in assets]
        assert refs == [
            "Склад №12",
            "Черникова №27 (1 из 3)",
            "Черникова №27 (2 из 3)",
            "Черникова №27 (3 из 3)",
        ]
        # Каждая единица несёт СВОЮ стоимость, а не стоимость всей строки описи.
        assert {asset.initial_cost for asset in assets if asset.name == "Ларь морозильный"} == {
            Decimal("30000.00")
        }
        assert len({asset.inventory_number for asset in assets}) == 4
        # СПИ карточке не проставляется — срок приходит из категории.
        assert all(asset.useful_life_months is None for asset in assets)
        assert all(asset.location_id is not None for asset in assets)

        second = await run(session, path, dry_run=False)
        assert (second["created"], second["skipped"]) == (0, 4)


async def test_correction_rewrites_the_residual_tail(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Правка прошлого месяца пересчитывает остатки всех следующих.

    ``residual_after`` хранится, а не считается на лету. Без пересчёта хвоста правка августа
    оставила бы сентябрь и октябрь с остатками, посчитанными от старой суммы, — и баланс
    показывал бы цифру, которой ни в одной строке нет.
    """
    async with async_session_factory() as session:
        asset = await _asset(session, cost="120000.00", life=120)
        await session.commit()

        for month in (date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)):
            await accrue_depreciation(session, period_month=month, asset_id=asset.id)
        await session.commit()

        entry = await correct_depreciation(
            session,
            asset_id=asset.id,
            period_month=date(2026, 1, 1),
            amount=Decimal("400.00"),
            note="Объект введён позже, чем стояло в карточке",
        )
        await session.commit()

        assert entry.is_manual is True
        assert entry.corrected_at is not None

        tail = (
            await session.scalars(
                select(DepreciationEntry)
                .where(DepreciationEntry.asset_id == asset.id)
                .order_by(DepreciationEntry.period_month)
            )
        ).all()
        # 120 000 − 400 − 1 000 − 1 000: хвост поехал на 600 ₽, которые не начислили в январе.
        assert [row.residual_after for row in tail] == [
            Decimal("119600.00"),
            Decimal("118600.00"),
            Decimal("117600.00"),
        ]
        assert await residual_value(session, asset) == Decimal("117600.00")


async def test_correction_cannot_exceed_initial_cost(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Объект нельзя самортизировать сверх первоначальной стоимости — иначе актив уйдёт в минус."""
    async with async_session_factory() as session:
        asset = await _asset(session, cost="10000.00", life=10)
        await session.commit()

        await accrue_depreciation(session, period_month=date(2026, 1, 1), asset_id=asset.id)
        await accrue_depreciation(session, period_month=date(2026, 2, 1), asset_id=asset.id)
        await session.commit()

        with pytest.raises(FixedAssetError):
            await correct_depreciation(
                session,
                asset_id=asset.id,
                period_month=date(2026, 2, 1),
                amount=Decimal("9500.00"),  # плюс уже начисленная тысяча января — перебор
            )


async def test_month_close_journals_the_run_and_is_repeatable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Закрытие месяца пишет прогон в журнал, а повтор не задваивает и не затирает правки.

    Результат ночного закрытия в логах контейнера не переживёт пересборку, а на вопрос
    «почему за месяц начислено столько» нужно уметь ответить и через полгода.
    """
    async with async_session_factory() as session:
        first = await _asset(session, cost="120000.00", life=120)
        await _asset(session, cost="60000.00", life=60)
        await session.commit()

        run = await close_month(session, period_month=date(2026, 2, 1))
        assert run.agent_name == "fixed_assets_month_close"
        assert run.status == "success"
        assert run.finished_at is not None
        assert run.result == {"entries": 2, "amount": "2000.00"}

        # Правка вручную, затем повторное закрытие того же месяца.
        await correct_depreciation(
            session,
            asset_id=first.id,
            period_month=date(2026, 2, 1),
            amount=Decimal("250.00"),
        )
        await session.commit()

        again = await close_month(session, period_month=date(2026, 2, 1), reason="manual")
        assert again.result == {"entries": 0, "amount": "0.00"}

        corrected = await session.scalar(
            select(DepreciationEntry).where(
                DepreciationEntry.asset_id == first.id,
                DepreciationEntry.period_month == date(2026, 2, 1),
            )
        )
        assert corrected.amount == Decimal("250.00")
        assert corrected.is_manual is True


def test_job_closes_the_month_that_just_ended() -> None:
    """1-го числа закрывается ПРОШЕДШИЙ месяц: за идущий начислять нечего."""
    assert previous_month(date(2026, 9, 1)) == date(2026, 8, 1)
    assert previous_month(date(2027, 1, 1)) == date(2026, 12, 1)
    # Ручной запуск в середине месяца ведёт себя так же — закрывает предыдущий.
    assert previous_month(date(2026, 9, 17)) == date(2026, 8, 1)

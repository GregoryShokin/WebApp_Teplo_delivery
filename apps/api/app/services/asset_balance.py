"""Строки баланса и строка ОПиУ по основным средствам — готовые к переносу в отчётность.

Балансового модуля и ОПиУ в приложении нет: они ведутся в таблицах, и по методологии реестра
«эта цифра переносится вручную пару раз в год». Здесь модуль ОС отдаёт ровно те числа, которые
в отчётность и переносят: остаточную стоимость по одиннадцати строкам внеоборотных активов и
помесячную амортизацию для строки «УчОС Амортизация».

ОДИННАДЦАТЬ СТРОК ПРИ ДЕСЯТИ КАТЕГОРИЯХ. «Не работающее оборудование» — это СТАТУС карточки,
а не категория (решение владельца 2026-05-25). Объект со статусом ``not_working`` уходит в свою
строку и ИСКЛЮЧАЕТСЯ из строки своей категории: иначе холодильная витрина по стоимости лома
посчиталась бы и там, и там, а сумма строк перестала бы сходиться с реестром.

ПОЧЕМУ ОСТАТОК СЧИТАЕТСЯ ОТ НАЧИСЛЕНИЙ, А НЕ ИЗ ``residual_after``. Хранимый остаток — снимок
на момент записи, и его переписывает ``recompute_residuals`` при любой правке прошлого. Для
отчёта на дату нужен остаток НА ЭТУ ДАТУ, поэтому он выводится заново: первоначальная минус
сумма начислений по включительно этот месяц.

ПОЧЕМУ СНИМОК. Первоначальная стоимость тоже меняется задним числом — ручной коррекцией,
применённой переоценкой и правкой карточки. Без снимка баланс за июль, уже перенесённый в
отчётность, в сентябре тихо стал бы другим. Поэтому закрытие месяца замораживает строки, а
``compare_with_snapshot`` показывает, если прошлое всё-таки сдвинули.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AssetBalanceSnapshot,
    AssetCategory,
    AssetMovement,
    DepreciationEntry,
    FixedAsset,
)
from app.services.asset_disposal import WRITEOFF

# Строка для объектов, которые не работают. Собирается по СТАТУСУ и категории не имеет —
# отсюда текстовый ключ строки вместо ссылки на справочник.
NOT_WORKING_LINE = "Не работающее оборудование"

# Объект, выбывший из учёта, в баланс не входит: его стоимость уже ушла из внеоборотных активов.
DISPOSED_STATUSES = ("disposed", "sold")


@dataclass(frozen=True)
class BalanceLine:
    line_name: str
    category_id: object | None
    asset_count: int
    initial_cost: Decimal
    accumulated: Decimal
    residual: Decimal
    depreciation: Decimal


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def month_start(value: date) -> date:
    return value.replace(day=1)


def _has_left(asset: FixedAsset, period: date, disposed_on: dict[object, date]) -> bool:
    """Ушёл ли объект из внеоборотных активов к концу указанного месяца."""
    if asset.status not in DISPOSED_STATUSES:
        return False
    left = disposed_on.get(asset.id)
    # Строки выбытия нет — про дату не известно ничего; исключаем всегда, как делали раньше.
    return True if left is None else left <= period


async def balance_lines(session: AsyncSession, *, as_of: date) -> list[BalanceLine]:
    """Строки баланса по ОС на конец указанного месяца.

    ``as_of`` — любая дата внутри месяца; берётся месяц целиком. Накопленная амортизация
    считается по начислениям ВКЛЮЧИТЕЛЬНО по этот месяц, поэтому отчёт за июль не поедет
    от того, что август уже закрыт.

    ВЫБЫТИЕ УЧИТЫВАЕТСЯ ПО ЕГО МЕСЯЦУ, А НЕ ПО ТЕКУЩЕМУ СТАТУСУ. Фильтр по статусу здесь
    смотрел бы на сегодня: объект, списанный в августе, задним числом исчезал бы и из июля —
    то есть из месяца, который уже заморожен и перенесён в отчётность. Поэтому объект со
    строкой выбытия покидает баланс начиная С МЕСЯЦА ВЫБЫТИЯ и стоит в предыдущих как живой.
    Карточки, выбывшие без строки (продажа и всё, что списали до появления этого контура),
    исключаются как раньше — про них не известно когда, и гадать хуже, чем оставить как было.
    """
    period = month_start(as_of)

    assets = (await session.scalars(select(FixedAsset))).all()
    disposed_on = {
        asset_id: month_start(occurred_on)
        for asset_id, occurred_on in (
            await session.execute(
                select(AssetMovement.asset_id, AssetMovement.occurred_on).where(
                    AssetMovement.movement_type == WRITEOFF
                )
            )
        ).all()
    }
    assets = [asset for asset in assets if not _has_left(asset, period, disposed_on)]
    categories = {row.id: row for row in (await session.scalars(select(AssetCategory))).all()}

    accrued_rows = (
        await session.execute(
            select(DepreciationEntry.asset_id, func.sum(DepreciationEntry.amount))
            .where(DepreciationEntry.period_month <= period)
            .group_by(DepreciationEntry.asset_id)
        )
    ).all()
    accrued = {asset_id: _money(total) for asset_id, total in accrued_rows}

    month_rows = (
        await session.execute(
            select(DepreciationEntry.asset_id, func.sum(DepreciationEntry.amount))
            .where(DepreciationEntry.period_month == period)
            .group_by(DepreciationEntry.asset_id)
        )
    ).all()
    monthly = {asset_id: _money(total) for asset_id, total in month_rows}

    buckets: dict[str, dict[str, object]] = {}
    for asset in assets:
        # Неработающее — своя строка, и из строки своей категории оно ИСКЛЮЧАЕТСЯ: иначе
        # посчиталось бы дважды и сумма строк разошлась бы с реестром.
        if asset.status == "not_working":
            line_name = NOT_WORKING_LINE
            category_id = None
        else:
            category = categories.get(asset.category_id) if asset.category_id else None
            line_name = category.name if category else "Без категории"
            category_id = category.id if category else None

        bucket = buckets.setdefault(
            line_name,
            {
                "category_id": category_id,
                "count": 0,
                "initial": Decimal("0.00"),
                "accumulated": Decimal("0.00"),
                "depreciation": Decimal("0.00"),
            },
        )
        initial = _money(asset.initial_cost)
        got = accrued.get(asset.id, Decimal("0.00"))
        bucket["count"] = int(bucket["count"]) + 1  # type: ignore[call-overload]
        bucket["initial"] = _money(bucket["initial"]) + initial  # type: ignore[operator]
        bucket["accumulated"] = _money(bucket["accumulated"]) + got  # type: ignore[operator]
        bucket["depreciation"] = _money(bucket["depreciation"]) + monthly.get(  # type: ignore[operator]
            asset.id, Decimal("0.00")
        )

    lines = [
        BalanceLine(
            line_name=name,
            category_id=data["category_id"],
            asset_count=int(data["count"]),  # type: ignore[call-overload]
            initial_cost=_money(data["initial"]),
            accumulated=_money(data["accumulated"]),
            residual=max(_money(data["initial"]) - _money(data["accumulated"]), Decimal("0.00")),
            depreciation=_money(data["depreciation"]),
        )
        for name, data in buckets.items()
    ]
    # Неработающее — последней строкой: в балансе это хвост внеоборотных активов, а не
    # равноправная категория.
    return sorted(lines, key=lambda item: (item.line_name == NOT_WORKING_LINE, item.line_name))


async def snapshot_month(
    session: AsyncSession, *, period_month: date
) -> list[AssetBalanceSnapshot]:
    """Заморозить строки баланса за закрытый месяц.

    Идемпотентно: повторный вызов ПЕРЕПИСЫВАЕТ снимок. Это осознанно — снимок делается сразу
    после начисления, и если месяц закрывают повторно (доначислили забытый объект), цифра
    должна догнать реальность. Защита от тихого дрейфа не здесь, а в ``compare_with_snapshot``:
    она сравнивает снимок с текущим состоянием и показывает расхождение владельцу.
    """
    period = month_start(period_month)
    lines = await balance_lines(session, as_of=period)

    existing = {
        row.line_name: row
        for row in (
            await session.scalars(
                select(AssetBalanceSnapshot).where(AssetBalanceSnapshot.period_month == period)
            )
        ).all()
    }

    saved: list[AssetBalanceSnapshot] = []
    for line in lines:
        row = existing.pop(line.line_name, None)
        if row is None:
            row = AssetBalanceSnapshot(period_month=period, line_name=line.line_name)
            session.add(row)
        row.category_id = line.category_id  # type: ignore[assignment]
        row.asset_count = line.asset_count
        row.initial_cost = line.initial_cost
        row.accumulated = line.accumulated
        row.residual = line.residual
        row.depreciation = line.depreciation
        saved.append(row)

    # Строка исчезла (последний объект категории списан) — снимок не должен её хранить.
    for orphan in existing.values():
        await session.delete(orphan)

    await session.flush()
    return saved


@dataclass(frozen=True)
class LineDrift:
    line_name: str
    field: str
    snapshot_value: Decimal
    current_value: Decimal


async def compare_with_snapshot(session: AsyncSession, *, period_month: date) -> list[LineDrift]:
    """Сравнить замороженный месяц с тем, что получается сейчас.

    Прошлое двигают три пути: ручная коррекция начисления, применённая переоценка по сообщению
    менеджера и правка первоначальной стоимости в карточке. Все три законны, но отчётность за
    закрытый месяц уже ушла владельцу — и он должен узнать, что цифра изменилась, а не
    обнаружить это при годовой сверке.

    Пустой список означает, что перенесённые числа всё ещё верны.
    """
    period = month_start(period_month)
    snapshot = {
        row.line_name: row
        for row in (
            await session.scalars(
                select(AssetBalanceSnapshot).where(AssetBalanceSnapshot.period_month == period)
            )
        ).all()
    }
    if not snapshot:
        return []

    drift: list[LineDrift] = []
    current = {line.line_name: line for line in await balance_lines(session, as_of=period)}

    for name, row in snapshot.items():
        live = current.get(name)
        if live is None:
            drift.append(LineDrift(name, "строка исчезла", _money(row.residual), Decimal("0.00")))
            continue
        for field, was, now in (
            ("остаточная", _money(row.residual), live.residual),
            ("амортизация за месяц", _money(row.depreciation), live.depreciation),
        ):
            if was != now:
                drift.append(LineDrift(name, field, was, now))

    for name in current.keys() - snapshot.keys():
        drift.append(LineDrift(name, "строка появилась", Decimal("0.00"), current[name].residual))
    return drift


async def depreciation_series(
    session: AsyncSession, *, date_from: date | None = None, date_to: date | None = None
) -> list[tuple[date, Decimal]]:
    """Помесячный ряд амортизации по всем объектам — строка «УчОС Амортизация» в ОПиУ.

    Именно та цифра, которую по методологии реестра «переносят вручную пару раз в год».
    """
    query = select(DepreciationEntry.period_month, func.sum(DepreciationEntry.amount)).group_by(
        DepreciationEntry.period_month
    )
    if date_from is not None:
        query = query.where(DepreciationEntry.period_month >= month_start(date_from))
    if date_to is not None:
        query = query.where(DepreciationEntry.period_month <= month_start(date_to))
    rows = (await session.execute(query.order_by(DepreciationEntry.period_month))).all()
    return [(period, _money(total)) for period, total in rows]

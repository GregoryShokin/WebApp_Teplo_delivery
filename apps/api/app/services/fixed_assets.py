"""Амортизация основных средств и правило «ремонт vs модернизация».

Метод один для всех категорий — ЛИНЕЙНЫЙ ПОМЕСЯЧНЫЙ (решение владельца): месячная сумма =
первоначальная стоимость / срок полезного использования в месяцах. Начисление идёт с месяца
ввода в эксплуатацию и прекращается, когда остаточная стоимость дошла до нуля либо объект выбыл.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AssetCategory, DepreciationEntry, FixedAsset
from app.models.fixed_asset import UPGRADE_SHARE_THRESHOLD

# Объект, выбывший из учёта, больше не амортизируется.
INACTIVE_STATUSES = ("disposed", "sold")


class FixedAssetError(Exception):
    """Ошибка контура учёта ОС, понятная пользователю."""


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def month_start(value: date) -> date:
    return value.replace(day=1)


async def resolve_useful_life(session: AsyncSession, asset: FixedAsset) -> int | None:
    """СПИ объекта: собственный срок карточки важнее срока категории."""
    if asset.useful_life_months:
        return asset.useful_life_months
    if asset.category_id is None:
        return None
    return await session.scalar(
        select(AssetCategory.useful_life_months).where(AssetCategory.id == asset.category_id)
    )


async def accumulated_depreciation(session: AsyncSession, asset_id: uuid.UUID) -> Decimal:
    total = await session.scalar(
        select(func.coalesce(func.sum(DepreciationEntry.amount), 0)).where(
            DepreciationEntry.asset_id == asset_id
        )
    )
    return _money(total)


async def residual_value(session: AsyncSession, asset: FixedAsset) -> Decimal:
    """Остаточная стоимость: первоначальная минус накопленная амортизация."""
    accrued = await accumulated_depreciation(session, asset.id)
    return max(_money(asset.initial_cost) - accrued, Decimal("0.00"))


async def accrue_depreciation(
    session: AsyncSession, *, period_month: date, asset_id: uuid.UUID | None = None
) -> list[DepreciationEntry]:
    """Начислить амортизацию за месяц. Идемпотентно: повторный прогон ничего не добавит.

    Пропускаются объекты, которые ещё не введены в эксплуатацию (``commissioned_on`` пуст или
    относится к будущему месяцу), выбывшие, без СПИ и уже самортизированные полностью.

    Последний месяц срока закрывается ОСТАТКОМ, а не расчётной долей: при делении на срок
    копейки округления накапливаются, и без этого у объекта вечно висел бы хвост в несколько
    копеек, который не даёт остаточной стоимости дойти до нуля.
    """
    period = month_start(period_month)
    query = select(FixedAsset).where(FixedAsset.status.not_in(INACTIVE_STATUSES))
    if asset_id is not None:
        query = query.where(FixedAsset.id == asset_id)
    assets = (await session.scalars(query)).all()

    created: list[DepreciationEntry] = []
    for asset in assets:
        if asset.commissioned_on is None or month_start(asset.commissioned_on) > period:
            continue
        life = await resolve_useful_life(session, asset)
        if not life:
            continue

        exists = await session.scalar(
            select(DepreciationEntry.id).where(
                DepreciationEntry.asset_id == asset.id,
                DepreciationEntry.period_month == period,
            )
        )
        if exists is not None:
            continue

        residual = await residual_value(session, asset)
        if residual <= 0:
            continue

        planned = _money(_money(asset.initial_cost) / Decimal(life))
        amount = min(planned, residual) if planned > 0 else residual
        # Хвост меньше планового взноса дотягиваем сразу — иначе остаётся вечная копейка.
        if residual - amount < planned:
            amount = residual

        entry = DepreciationEntry(
            asset_id=asset.id,
            period_month=period,
            amount=amount,
            residual_after=_money(residual - amount),
        )
        session.add(entry)
        created.append(entry)

    await session.flush()
    return created


def classify_asset_expense(initial_cost: Decimal, expense_amount: Decimal) -> str:
    """Ремонт, модернизация или решение владельца — по доле расхода от первоначальной стоимости.

    Правило владельца: меньше 15% — ремонт (расход периода), больше 15% — модернизация
    (капитализируется и меняет базу амортизации), РОВНО 15% — спорная зона, решает владелец.
    """
    base = _money(initial_cost)
    if base <= 0:
        return "requires_owner_review"
    # Долю НЕ округляем: 14 999 из 100 000 — это 0,14999, честный ремонт, а любое округление
    # до сотых/тысячных превратило бы его ровно в 15% и увело на разбор владельцу.
    share = _money(expense_amount) / base
    if share == UPGRADE_SHARE_THRESHOLD:
        return "requires_owner_review"
    return "upgrade" if share > UPGRADE_SHARE_THRESHOLD else "repair"


async def capitalize_upgrade(
    session: AsyncSession, *, asset: FixedAsset, amount: Decimal
) -> FixedAsset:
    """Модернизация увеличивает первоначальную стоимость — дальше амортизируется новая база.

    Уже начисленную амортизацию не пересчитываем задним числом: прошлые месяцы закрыты, а
    остаточная стоимость честно вырастет на сумму модернизации.
    """
    if amount <= 0:
        raise FixedAssetError("Сумма модернизации должна быть положительной")
    asset.initial_cost = _money(asset.initial_cost) + _money(amount)
    await session.flush()
    return asset

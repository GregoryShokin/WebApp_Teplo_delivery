"""Owner-approved dated additions, shared by cards, depreciation and asset reporting.

Never change FixedAsset.initial_cost or old DepreciationEntry rows here. A decided
payment/link is protected against reclassification, deletion and duplicate application.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AccountingPeriodClose,
    AssetBalanceSnapshot,
    AssetCapitalization,
    AssetCashflowLink,
    AssetConditionReport,
    CashflowTransaction,
    DdsArticle,
    DepreciationEntry,
    FixedAsset,
    User,
)
from app.services.clock import MOSCOW_TZ


class CapitalizationError(ValueError):
    pass


def today() -> date:
    return datetime.now(MOSCOW_TZ).date()


def next_month(day: date) -> date:
    return date(day.year + (day.month == 12), 1 if day.month == 12 else day.month + 1, 1)


def month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


async def accounting_lock(session: AsyncSession) -> None:
    """Serialize owner decisions with accruals and frozen asset reports."""
    await session.execute(select(func.pg_advisory_xact_lock(875216041)))


async def additions(
    session: AsyncSession, asset_ids: list, *, on: date, depreciation: bool = False
) -> dict:
    if not asset_ids:
        return {}
    column = (
        AssetCapitalization.depreciation_from if depreciation else AssetCapitalization.effective_on
    )
    rows = (
        await session.execute(
            select(AssetCapitalization.asset_id, func.sum(AssetCapitalization.amount))
            .where(
                AssetCapitalization.asset_id.in_(asset_ids),
                AssetCapitalization.status == "confirmed",
                column <= on,
            )
            .group_by(AssetCapitalization.asset_id)
        )
    ).all()
    return {aid: Decimal(str(total)) for aid, total in rows}


async def cost_at(
    session: AsyncSession, asset: FixedAsset, *, on: date | None = None, depreciation: bool = False
) -> Decimal:
    values = await additions(session, [asset.id], on=on or today(), depreciation=depreciation)
    return Decimal(str(asset.initial_cost)) + values.get(asset.id, Decimal("0"))


async def has_decisions(session: AsyncSession, asset_id: uuid.UUID) -> bool:
    return bool(
        await session.scalar(
            select(AssetCapitalization.id).where(AssetCapitalization.asset_id == asset_id).limit(1)
        )
    )


async def valuation_locked(session: AsyncSession, asset_id: uuid.UUID) -> bool:
    if await has_decisions(session, asset_id):
        return True
    return bool(
        await session.scalar(
            select(AssetCashflowLink.id)
            .join(
                CashflowTransaction,
                CashflowTransaction.id == AssetCashflowLink.cashflow_transaction_id,
            )
            .join(DdsArticle, DdsArticle.id == CashflowTransaction.article_id)
            .where(AssetCashflowLink.asset_id == asset_id, DdsArticle.asset_link_kind == "repair")
            .limit(1)
        )
    )


async def protect_transaction(session: AsyncSession, transaction_id: uuid.UUID) -> None:
    # Lock the money row as well as its source link. Confirmations acquire the same lock.
    await session.scalar(
        select(CashflowTransaction)
        .where(CashflowTransaction.id == transaction_id)
        .with_for_update()
    )
    decided = await session.scalar(
        select(AssetCapitalization.id)
        .join(AssetCashflowLink, AssetCashflowLink.id == AssetCapitalization.link_id)
        .where(AssetCashflowLink.cashflow_transaction_id == transaction_id)
        .limit(1)
    )
    if decided:
        raise CapitalizationError(
            "По ремонту уже принято решение в карточке оборудования. Изменять,"
            " исключать или удалять его платёж нельзя."
        )


async def _candidate(session: AsyncSession, asset_id: uuid.UUID, link_id: uuid.UUID):
    link = await session.scalar(
        select(AssetCashflowLink).where(
            AssetCashflowLink.id == link_id, AssetCashflowLink.asset_id == asset_id
        )
    )
    if link is None:
        raise CapitalizationError("Связь ремонта с оборудованием не найдена")
    tx = await session.scalar(
        select(CashflowTransaction)
        .where(CashflowTransaction.id == link.cashflow_transaction_id)
        .with_for_update()
    )
    link = await session.scalar(
        select(AssetCashflowLink)
        .where(AssetCashflowLink.id == link_id, AssetCashflowLink.asset_id == asset_id)
        .with_for_update()
    )
    if link is None:
        raise CapitalizationError("Связь ремонта была изменена, обновите карточку")
    if tx is None or tx.quality_status == "excluded" or tx.direction != "out":
        raise CapitalizationError("Платёж отсутствует или исключён из учёта")
    article = await session.get(DdsArticle, tx.article_id) if tx.article_id else None
    if (
        article is None
        or article.asset_link_kind != "repair"
        or link.kind not in ("repair", "upgrade")
    ):
        raise CapitalizationError(
            "Подтверждать можно только платёж по статье капитального ремонта ОС"
        )
    if Decimal(str(link.amount)) <= 0 or Decimal(str(link.amount)) > Decimal(str(tx.amount)):
        raise CapitalizationError("Сумма ремонтной связи не соответствует платежу")
    return link, tx


async def preview(session: AsyncSession, asset: FixedAsset, link_id: uuid.UUID, when: date) -> dict:
    from app.services.fixed_assets import resolve_useful_life

    link, tx = await _candidate(session, asset.id, link_id)
    from app.services.asset_analytics import AssetLinkError, resolve_asset_context

    article = await session.get(DdsArticle, tx.article_id)
    try:
        await resolve_asset_context(
            session, article=article, asset_id=asset.id, amount=Decimal(str(link.amount))
        )
    except AssetLinkError as exc:
        raise CapitalizationError(str(exc)) from exc
    if asset.status in ("sold", "disposed"):
        raise CapitalizationError("Оборудование уже выбыло из учёта")
    if when > today():
        raise CapitalizationError("Дата применения не может быть в будущем")
    if when < tx.operation_date or (asset.commissioned_on and when < asset.commissioned_on):
        raise CapitalizationError(
            "Дата применения не может быть раньше платежа или ввода оборудования в эксплуатацию"
        )
    period = when.replace(day=1)
    for model in (AssetBalanceSnapshot, AccountingPeriodClose):
        if await session.scalar(select(model.id).where(model.period_month >= period).limit(1)):
            raise CapitalizationError(
                "Дата затрагивает закрытый месяц. Выберите дату после последнего закрытого месяца."
            )
    start = next_month(when)
    if await session.scalar(
        select(DepreciationEntry.id)
        .where(DepreciationEntry.asset_id == asset.id, DepreciationEntry.period_month >= period)
        .limit(1)
    ):
        raise CapitalizationError(
            "Месяц применения или последующие месяцы уже начислены. Выберите "
            "более позднюю дату в неначисленном месяце."
        )
    # Do not date an approval before a later approval; displayed before/after must be stable.
    if await session.scalar(
        select(AssetCapitalization.id)
        .where(
            AssetCapitalization.asset_id == asset.id,
            AssetCapitalization.status == "confirmed",
            AssetCapitalization.effective_on > when,
        )
        .limit(1)
    ):
        raise CapitalizationError(
            "У оборудования уже есть более поздняя капитализация. Выберите дату не раньше неё."
        )
    cost = await cost_at(session, asset, on=when)
    accrued = Decimal(
        str(
            await session.scalar(
                select(func.coalesce(func.sum(DepreciationEntry.amount), 0)).where(
                    DepreciationEntry.asset_id == asset.id, DepreciationEntry.period_month <= period
                )
            )
            or 0
        )
    )
    life = await resolve_useful_life(session, asset)
    before_base = await cost_at(session, asset, on=start, depreciation=True)
    amount = Decimal(str(link.amount))
    new_cost = cost + amount
    old_residual = max(cost - accrued, Decimal("0"))
    new_residual = max(new_cost - accrued, Decimal("0"))
    enabled = (
        asset.commissioned_on is not None
        and life is not None
        and asset.status not in ("sold", "disposed", "not_working")
    )
    return dict(
        amount=amount,
        effective_on=when,
        depreciation_from=start,
        cost_before=cost,
        cost_after=new_cost,
        residual_before=old_residual,
        residual_after=new_residual,
        monthly_before=min(
            (before_base / Decimal(life)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP),
            old_residual,
        )
        if enabled
        else Decimal("0"),
        monthly_after=min(
            ((before_base + amount) / Decimal(life)).quantize(
                Decimal(".01"), rounding=ROUND_HALF_UP
            ),
            new_residual,
        )
        if enabled
        else Decimal("0"),
        useful_life_months=life,
    )


async def decide(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    link_id: uuid.UUID,
    status: str,
    when: date | None,
    user_id: uuid.UUID,
    note: str | None = None,
) -> AssetCapitalization:
    await accounting_lock(session)
    asset = await session.scalar(
        select(FixedAsset).where(FixedAsset.id == asset_id).with_for_update()
    )
    if asset is None:
        raise CapitalizationError("Оборудование не найдено")
    # Asset lock serializes decisions for different repairs on the same equipment.
    existing = await session.scalar(
        select(AssetCapitalization).where(
            AssetCapitalization.link_id == link_id, AssetCapitalization.asset_id == asset_id
        )
    )
    if existing:
        if existing.status != status or existing.effective_on != when:
            raise CapitalizationError("По этому ремонту уже принято другое решение")
        return existing
    link, tx = await _candidate(session, asset_id, link_id)
    figures = {}
    if status == "confirmed":
        if when is None:
            raise CapitalizationError("Укажите дату применения")
        figures = await preview(session, asset, link_id, when)
    elif status != "dismissed" or when is not None:
        raise CapitalizationError("Некорректное решение")
    decision = AssetCapitalization(
        asset_id=asset_id,
        link_id=link_id,
        status=status,
        amount=link.amount,
        effective_on=when,
        depreciation_from=next_month(when) if when else None,
        decided_by_user_id=user_id,
        note=note,
        **{
            key: figures.get(key)
            for key in ("cost_before", "cost_after", "monthly_before", "monthly_after")
        },
    )
    session.add(decision)
    await session.flush()
    # Only remove the repair warning; a condition-report decision is independent.
    pending = await session.scalar(
        select(AssetCashflowLink.id)
        .join(
            CashflowTransaction, CashflowTransaction.id == AssetCashflowLink.cashflow_transaction_id
        )
        .join(DdsArticle, DdsArticle.id == CashflowTransaction.article_id)
        .where(
            AssetCashflowLink.asset_id == asset_id,
            DdsArticle.asset_link_kind == "repair",
            ~select(AssetCapitalization.id)
            .where(AssetCapitalization.link_id == AssetCashflowLink.id)
            .exists(),
        )
        .limit(1)
    )
    condition = await session.scalar(
        select(AssetConditionReport.id)
        .where(
            AssetConditionReport.asset_id == asset_id,
            AssetConditionReport.status.in_(["pending", "proposed"]),
        )
        .limit(1)
    )
    if (
        not pending
        and not condition
        and asset.review_reason
        and ("модернизац" in asset.review_reason or "капитальн" in asset.review_reason)
    ):
        asset.review_status = "ok"
        asset.review_reason = None
    return decision


async def history(session: AsyncSession, asset_id: uuid.UUID) -> list[dict]:
    rows = (
        await session.execute(
            select(
                AssetCashflowLink,
                CashflowTransaction,
                DdsArticle.name,
                DdsArticle.asset_link_kind,
                AssetCapitalization,
                User.full_name,
            )
            .join(
                CashflowTransaction,
                CashflowTransaction.id == AssetCashflowLink.cashflow_transaction_id,
            )
            .outerjoin(DdsArticle, DdsArticle.id == CashflowTransaction.article_id)
            .outerjoin(AssetCapitalization, AssetCapitalization.link_id == AssetCashflowLink.id)
            .outerjoin(User, User.id == AssetCapitalization.decided_by_user_id)
            .where(
                AssetCashflowLink.asset_id == asset_id,
                AssetCashflowLink.kind.in_(["repair", "upgrade"]),
            )
            .order_by(
                CashflowTransaction.operation_date.desc(), AssetCashflowLink.created_at.desc()
            )
        )
    ).all()
    return [
        dict(
            link_id=link.id,
            transaction_id=tx.id,
            operation_date=tx.operation_date,
            amount=Decimal(str(link.amount)),
            article_name=name or "Статья не указана",
            payment_purpose=tx.payment_purpose,
            status=decision.status
            if decision
            else (
                "pending" if kind == "repair" and tx.quality_status != "excluded" else "maintenance"
            ),
            effective_on=decision.effective_on if decision else None,
            depreciation_from=decision.depreciation_from if decision else None,
            decided_at=decision.created_at if decision else None,
            decided_by=actor,
            note=decision.note if decision else None,
            cost_before=decision.cost_before if decision else None,
            cost_after=decision.cost_after if decision else None,
            monthly_after=decision.monthly_after if decision else None,
        )
        for link, tx, name, kind, decision, actor in rows
    ]

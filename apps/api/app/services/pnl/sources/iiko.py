"""Чтение месячных фактов iiko из зеркала.

Отчёт НИКОГДА не ходит в iiko синхронно: зеркало наполняет ночная джоба, а страница делает
один SELECT. Пустое зеркало — это «нет данных», а не ошибка: на локали и превью iiko Server
недоступен по адресу, и отчёт обязан честно об этом сказать, а не показать нули.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pnl import (
    PnlIikoFact,
    PnlIikoProductObservation,
    PnlPartnerCommissionFact,
    PnlProductMonthlyDecision,
    PnlProductWhitelist,
)
from app.services.pnl.revision_products import (
    load_revision_product_guids,
    normalize_iiko_product_guid,
)


@dataclass(slots=True)
class GoodsWorkupItem:
    product_guid: str
    product_name: str
    product_code: str | None
    source_kind: str
    source_amount: Decimal
    amount: Decimal
    rows_count: int
    synced_at: datetime


@dataclass(slots=True)
class PartnerCommissionItem:
    partner_name: str
    revenue_amount: Decimal
    commission_rate: Decimal
    commission_amount: Decimal
    rows_count: int
    source_ref: str
    synced_at: datetime


async def month_partner_commissions(
    session: AsyncSession, month_start: date
) -> list[PartnerCommissionItem]:
    rows = (
        await session.scalars(
            select(PnlPartnerCommissionFact)
            .where(PnlPartnerCommissionFact.period_month == month_start)
            .order_by(PnlPartnerCommissionFact.commission_amount.desc())
        )
    ).all()
    return [
        PartnerCommissionItem(
            partner_name=row.partner_name,
            revenue_amount=row.revenue_amount,
            commission_rate=row.commission_rate,
            commission_amount=row.commission_amount,
            rows_count=row.rows_count,
            source_ref=row.source_ref,
            synced_at=row.synced_at,
        )
        for row in rows
    ]


def workup_expense_amount(source_kind: str, amount: Decimal) -> Decimal:
    """Привести временную сумму к соглашению расходной строки ОПиУ."""
    return amount


async def month_workup_items(session: AsyncSession, month_start: date) -> list[GoodsWorkupItem]:
    """Товары, временно признанные расходом только в указанном месяце."""
    revision_product_guids = await load_revision_product_guids(session)
    stocked_rule_guids = set(
        (
            await session.execute(
                select(PnlProductWhitelist.iiko_product_guid).where(
                    PnlProductWhitelist.source_kind == "inventory",
                    PnlProductWhitelist.include_status == "stocked",
                )
            )
        ).scalars()
    )
    stocked_guids = revision_product_guids | {
        normalize_iiko_product_guid(guid) for guid in stocked_rule_guids
    }
    rows = (
        await session.execute(
            select(
                PnlProductMonthlyDecision.iiko_product_guid,
                PnlIikoProductObservation.product_name,
                PnlIikoProductObservation.product_code,
                PnlProductMonthlyDecision.source_kind,
                PnlIikoProductObservation.amount,
                PnlIikoProductObservation.rows_count,
                PnlIikoProductObservation.synced_at,
            )
            .join(
                PnlIikoProductObservation,
                and_(
                    PnlIikoProductObservation.period_month
                    == PnlProductMonthlyDecision.period_month,
                    PnlIikoProductObservation.iiko_product_guid
                    == PnlProductMonthlyDecision.iiko_product_guid,
                    PnlIikoProductObservation.source_kind == PnlProductMonthlyDecision.source_kind,
                ),
            )
            .where(
                PnlProductMonthlyDecision.period_month == month_start,
                PnlProductMonthlyDecision.decision_kind == "workup",
            )
            .order_by(PnlIikoProductObservation.product_name)
        )
    ).all()
    return [
        GoodsWorkupItem(
            product_guid=guid,
            product_name=name or guid,
            product_code=code,
            source_kind=source_kind,
            source_amount=source_amount,
            amount=workup_expense_amount(source_kind, source_amount),
            rows_count=rows_count,
            synced_at=synced_at,
        )
        for guid, name, code, source_kind, source_amount, rows_count, synced_at in rows
        if normalize_iiko_product_guid(guid) not in stocked_guids
    ]


async def month_facts(session: AsyncSession, month_start: date) -> dict[str, Decimal]:
    """Итоговые метрики месяца (direction='total') — код метрики → сумма."""
    rows = (
        await session.execute(
            select(PnlIikoFact.metric_code, PnlIikoFact.amount).where(
                PnlIikoFact.period_month == month_start,
                PnlIikoFact.direction == "total",
            )
        )
    ).all()
    return {code: amount for code, amount in rows}


async def month_by_direction(
    session: AsyncSession, month_start: date, metric: str
) -> dict[str, Decimal]:
    """Разрез метрики по направлениям — для будущих колонок Роллы/Пицца/ГЦ/Бар."""
    rows = (
        await session.execute(
            select(PnlIikoFact.direction, PnlIikoFact.amount).where(
                PnlIikoFact.period_month == month_start,
                PnlIikoFact.metric_code == metric,
                PnlIikoFact.direction != "total",
            )
        )
    ).all()
    return {direction: amount for direction, amount in rows}


async def synced_at(session: AsyncSession, month_start: date) -> datetime | None:
    """Когда зеркало последний раз обновлялось за этот месяц.

    Нужна на странице: молча упавшая джоба обязана выглядеть как «данных нет с такого-то
    числа», а не как отсутствие выручки.
    """
    return await session.scalar(
        select(PnlIikoFact.synced_at)
        .where(PnlIikoFact.period_month == month_start)
        .order_by(PnlIikoFact.synced_at.desc())
        .limit(1)
    )

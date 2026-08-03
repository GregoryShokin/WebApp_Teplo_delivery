"""Чтение месячных фактов iiko из зеркала.

Отчёт НИКОГДА не ходит в iiko синхронно: зеркало наполняет ночная джоба, а страница делает
один SELECT. Пустое зеркало — это «нет данных», а не ошибка: на локали и превью iiko Server
недоступен по адресу, и отчёт обязан честно об этом сказать, а не показать нули.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pnl import PnlIikoFact


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

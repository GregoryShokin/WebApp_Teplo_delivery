"""Ручные числа строк: то, что не заполняется автоматически ничем и никогда.

Шесть строк эталона не имеют машинного источника по природе, а не по недоделке: телефония
Mango (личный кабинет за SPA-логином и капчей), комиссия агрегатора (сумму присылают
менеджеры), бюджет контекстной рекламы из панели подрядчика, прочие доходы, проценты по
кредитам (статья ДДС не разделяет тело и проценты). Без ручного ввода эти строки остаются
пустыми навсегда, и ОПиУ никогда не сходится с таблицей владельца.

Ручное число — ПЕРВИЧКА, а не правка отчёта: оно живёт своей строкой с автором, датой и
обязательной причиной, а не подменяет вычисленную величину. Поэтому его нельзя перепутать с
посчитанным: у строки будет статус «ручной ввод», а в подсказке — кто и почему.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PnlManualEntry


async def build_manual_layer(
    session: AsyncSession, month_start: date
) -> dict[str, list[PnlManualEntry]]:
    """Ручные числа месяца, сгруппированные по строкам отчёта."""
    entries = (
        await session.execute(
            select(PnlManualEntry).where(PnlManualEntry.period_month == month_start)
        )
    ).scalars()
    grouped: dict[str, list[PnlManualEntry]] = defaultdict(list)
    for entry in entries:
        grouped[entry.line_code].append(entry)
    return grouped


def manual_total(entries: list[PnlManualEntry]) -> Decimal:
    return sum((entry.amount * entry.sign for entry in entries), Decimal("0.00"))

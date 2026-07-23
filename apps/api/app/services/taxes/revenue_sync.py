"""Заливка выручки iiko в ``iiko_revenue_period`` — база налога.

Идемпотентно: iiko правит закрытые дни (сторно, перепроведение), поэтому повторный прогон
за тот же период не создаёт дублей, а обновляет строку и пишет отличие в журнал
``iiko_revenue_revision``. Без журнала дрейф зафиксированного периода необъясним.

Два инварианта, которые сервис держит сам:

1. **Гранулярности не смешиваются внутри месяца.** Если заливаются подневные строки за
   месяц, месячная строка того же месяца удаляется в той же транзакции — иначе сумма
   задвоится. Обратно так же: месячная строка вытесняет ранее залитые дни этого месяца.
2. **Правки логируются только при реальном изменении суммы.** Повторная заливка тех же
   чисел журнал не засоряет.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tax import IikoRevenuePeriod, IikoRevenueRevision

ZERO = Decimal("0")


@dataclass(frozen=True)
class RevenueRecord:
    """Одна строка выручки на заливку. ``revenue_net`` — база налога (со скидками)."""

    period_start: date
    period_end: date
    granularity: str  # 'day' | 'month'
    department: str
    revenue_net: Decimal
    revenue_gross: Decimal | None = None
    discount_amount: Decimal | None = None
    source: str = "iiko_olap"


@dataclass
class SyncResult:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    superseded_rows: int = 0  # строки другой гранулярности, удалённые как вытесненные
    revisions: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "superseded_rows": self.superseded_rows,
            "revisions": self.revisions,
        }


def _q(value: Decimal | None) -> Decimal | None:
    return value.quantize(Decimal("0.01")) if value is not None else None


def _other_granularity(granularity: str) -> str:
    return "month" if granularity == "day" else "day"


def _month_key(day: date) -> tuple[int, int]:
    return day.year, day.month


async def sync_revenue_periods(
    session: AsyncSession,
    records: Iterable[RevenueRecord],
    *,
    sync_reason: str = "manual_sync",
    apply: bool = True,
) -> SyncResult:
    """Залить/обновить строки выручки. При ``apply=False`` считает, но не пишет (dry-run).

    ``sync_reason`` попадает в журнал правок: 'daily_job' | 'manual_sync' | 'backfill'.
    """
    records = list(records)
    result = SyncResult()

    # Месяцы, которые в этой заливке приходят подневно — чтобы вытеснить их месячные строки
    # (и наоборот). Собираем ДО записи, потому что вытеснение идёт по всему месяцу сразу.
    incoming_day_months: set[tuple[str, tuple[int, int]]] = set()
    incoming_month_starts: set[tuple[str, date]] = set()
    for rec in records:
        if rec.granularity == "day":
            incoming_day_months.add((rec.department, _month_key(rec.period_start)))
        else:
            incoming_month_starts.add((rec.department, rec.period_start))

    if apply:
        result.superseded_rows += await _supersede_conflicting(
            session,
            day_months=incoming_day_months,
            month_starts=incoming_month_starts,
        )

    for rec in records:
        existing = (
            await session.execute(
                select(IikoRevenuePeriod).where(
                    IikoRevenuePeriod.department == rec.department,
                    IikoRevenuePeriod.granularity == rec.granularity,
                    IikoRevenuePeriod.period_start == rec.period_start,
                )
            )
        ).scalar_one_or_none()

        net = _q(rec.revenue_net) or ZERO
        gross = _q(rec.revenue_gross)
        discount = _q(rec.discount_amount)

        if existing is None:
            result.inserted += 1
            if apply:
                session.add(
                    IikoRevenuePeriod(
                        period_start=rec.period_start,
                        period_end=rec.period_end,
                        granularity=rec.granularity,
                        department=rec.department,
                        revenue_net=net,
                        revenue_gross=gross,
                        discount_amount=discount,
                        source=rec.source,
                    )
                )
            continue

        if existing.revenue_net == net:
            result.unchanged += 1
            # Справочные поля могли измениться без изменения базы — подтянем тихо.
            if apply:
                existing.revenue_gross = gross
                existing.discount_amount = discount
            continue

        # База изменилась — это правка задним числом, логируем.
        result.updated += 1
        result.revisions += 1
        if apply:
            session.add(
                IikoRevenueRevision(
                    period_start=rec.period_start,
                    granularity=rec.granularity,
                    department=rec.department,
                    revenue_net_old=existing.revenue_net,
                    revenue_net_new=net,
                    sync_reason=sync_reason,
                )
            )
            existing.revenue_net = net
            existing.revenue_gross = gross
            existing.discount_amount = discount

    if apply:
        await session.flush()
    return result


async def _supersede_conflicting(
    session: AsyncSession,
    *,
    day_months: set[tuple[str, tuple[int, int]]],
    month_starts: set[tuple[str, date]],
) -> int:
    """Удалить строки той гранулярности, что вытесняется входящей.

    Возвращает число удалённых строк. Делает это до записи новых, в той же транзакции.
    """
    removed = 0

    # Подневные данные вытесняют месячную строку своего месяца.
    for department, (year, month) in day_months:
        month_start = date(year, month, 1)
        res = await session.execute(
            delete(IikoRevenuePeriod).where(
                IikoRevenuePeriod.department == department,
                IikoRevenuePeriod.granularity == "month",
                IikoRevenuePeriod.period_start == month_start,
            )
        )
        removed += res.rowcount or 0

    # Месячная строка вытесняет ранее залитые дни этого месяца.
    for department, month_start in month_starts:
        last = _last_day_of_month(month_start)
        res = await session.execute(
            delete(IikoRevenuePeriod).where(
                IikoRevenuePeriod.department == department,
                IikoRevenuePeriod.granularity == "day",
                IikoRevenuePeriod.period_start >= month_start,
                IikoRevenuePeriod.period_start <= last,
            )
        )
        removed += res.rowcount or 0

    return removed


def _last_day_of_month(day: date) -> date:
    return date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])


def records_from_iiko_monthly(
    rows: Sequence[dict],
    *,
    year: int,
    month: int,
    department: str,
    net_field: str = "DishDiscountSumInt",
    gross_field: str = "DishSumInt",
    discount_field: str = "DiscountSum",
) -> RevenueRecord:
    """Свернуть строки месячной выгрузки iiko OLAP в одну месячную ``RevenueRecord``.

    В выгрузке ``revenue_by_direction`` строки идут по направлениям (Бар, Пицца, Суши…) —
    для налоговой базы они суммируются: налог считается от общей выручки, а не по цехам.
    """
    net = sum(Decimal(str(r.get(net_field) or 0)) for r in rows)
    gross = sum(Decimal(str(r.get(gross_field) or 0)) for r in rows)
    discount = sum(Decimal(str(r.get(discount_field) or 0)) for r in rows)
    period_start = date(year, month, 1)
    return RevenueRecord(
        period_start=period_start,
        period_end=_last_day_of_month(period_start),
        granularity="month",
        department=department,
        revenue_net=net,
        revenue_gross=gross,
        discount_amount=discount,
        source="iiko_olap",
    )

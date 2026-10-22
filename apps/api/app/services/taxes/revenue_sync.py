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

Источник по умолчанию — живой OLAP iiko (``sync_revenue_from_iiko``): ночная задача
планировщика и кнопка на странице «Налоги». Месячные строки января–июня 2026 залиты из
выгрузок ``revenue_by_direction`` скриптом ``sync_tax_revenue``; подневный запрос с теми же
фильтрами дал те же суммы до копейки (сверено на проде 22.10.2026 по январю и июню).
"""

from __future__ import annotations

import calendar
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tax import IikoRevenuePeriod, IikoRevenueRevision
from app.services.taxes.repository import DEFAULT_DEPARTMENT

ZERO = Decimal("0")

# Раньше 2026 года ИП был на патенте: база УСН не нужна, и окно подкачки её не тянет.
SYNC_FLOOR = date(2026, 1, 1)

DailyRevenueFetcher = Callable[[AsyncSession, date, date], Awaitable[dict[date, Decimal]]]


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


class RevenueSyncError(RuntimeError):
    """Подкачка выручки из iiko не удалась. База налога при этом не тронута."""


@dataclass
class IikoRevenueSyncReport:
    """Итог подкачки: окно, сколько дней пришло и по месяцам — для страницы и журнала."""

    date_from: date
    date_to: date
    days: int
    total: Decimal
    result: SyncResult
    months: dict[str, Decimal] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "date_from": self.date_from.isoformat(),
            "date_to": self.date_to.isoformat(),
            "days": self.days,
            "total": str(self.total),
            "months": {k: str(v) for k, v in self.months.items()},
            **self.result.as_dict(),
        }


async def revenue_sync_window(
    session: AsyncSession, *, today: date, department: str = DEFAULT_DEPARTMENT
) -> tuple[date, date]:
    """Окно подкачки: с начала прошлого месяца (iiko правит закрытые дни задним числом)
    по вчера — и раньше, если в году есть месяцы вовсе без выручки.

    Вторая половина правила — то, чего не хватило осенью 2026: выручку за январь–июнь
    залили руками, июль–сентябрь никто не догрузил, и расчёт УСН за III квартал показал 0.
    С этим правилом первый же ночной прогон сам догружает все пропущенные месяцы года.
    """
    date_to = today - timedelta(days=1)
    prev_month_start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    year_start = max(date(date_to.year, 1, 1), SYNC_FLOOR)
    covered = {
        (d.year, d.month)
        for d in (
            await session.scalars(
                select(IikoRevenuePeriod.period_start).where(
                    IikoRevenuePeriod.department == department,
                    IikoRevenuePeriod.period_start >= year_start,
                    IikoRevenuePeriod.period_start <= date_to,
                )
            )
        ).all()
    }
    date_from = prev_month_start
    cursor = year_start
    while cursor <= date_to:
        if (cursor.year, cursor.month) not in covered:
            date_from = min(date_from, cursor)
            break
        cursor = _last_day_of_month(cursor) + timedelta(days=1)
    return max(date_from, SYNC_FLOOR), date_to


async def _fetch_tax_revenue(
    session: AsyncSession, date_from: date, date_to: date
) -> dict[date, Decimal]:
    from app.services.iiko_revenue import fetch_daily_revenue

    return await fetch_daily_revenue(session, date_from, date_to, exclude_deleted=True)


async def sync_revenue_from_iiko(
    session: AsyncSession,
    *,
    date_from: date,
    date_to: date,
    department: str = DEFAULT_DEPARTMENT,
    sync_reason: str = "daily_job",
    fetch: DailyRevenueFetcher | None = None,
) -> IikoRevenueSyncReport:
    """Подтянуть подневную выручку из iiko за окно и залить её в базу налога.

    Окно расширяется до начала месяца: подневные строки вытесняют месячную строку своего
    месяца, и неполный месяц дней занизил бы базу. День, который был в базе, а теперь
    из отчёта iiko пропал (заказы дня удалены), обнуляется с записью в журнал правок.

    Пустой ответ iiko — ошибка, а не «выручки не было»: разбор OLAP молча возвращает пустой
    словарь на любой сбой формата, и записать такое — значит тихо занизить налог.
    """
    date_from = date_from.replace(day=1)
    if date_from > date_to:
        raise RevenueSyncError(
            f"Пустое окно подкачки: {date_from.isoformat()} позже {date_to.isoformat()}."
        )
    try:
        by_day = await (fetch or _fetch_tax_revenue)(session, date_from, date_to)
    except SystemExit as exc:
        # IikoClient без адреса сервера бросает SystemExit — он пробивает обработчики
        # FastAPI и планировщика и роняет процесс целиком. Превращаем в обычную ошибку.
        raise RevenueSyncError("iiko не настроен на этом стенде — выручку взять негде.") from exc
    except Exception as exc:  # noqa: BLE001 - любой сбой iiko = база не тронута
        raise RevenueSyncError(f"iiko не ответил: {exc}") from exc

    by_day = {d: v for d, v in by_day.items() if date_from <= d <= date_to}
    if not by_day:
        raise RevenueSyncError(
            f"iiko вернул пустой отчёт за {date_from.strftime('%d.%m.%Y')}–"
            f"{date_to.strftime('%d.%m.%Y')} — база налога не тронута."
        )

    records = [
        RevenueRecord(
            period_start=d,
            period_end=d,
            granularity="day",
            department=department,
            revenue_net=v,
        )
        for d, v in sorted(by_day.items())
    ]
    stale_days = (
        await session.scalars(
            select(IikoRevenuePeriod.period_start).where(
                IikoRevenuePeriod.department == department,
                IikoRevenuePeriod.granularity == "day",
                IikoRevenuePeriod.period_start >= date_from,
                IikoRevenuePeriod.period_start <= date_to,
            )
        )
    ).all()
    records.extend(
        RevenueRecord(
            period_start=d, period_end=d, granularity="day", department=department,
            revenue_net=ZERO,
        )
        for d in sorted(set(stale_days) - set(by_day))
    )

    result = await sync_revenue_periods(session, records, sync_reason=sync_reason)
    months: dict[str, Decimal] = {}
    for d, v in by_day.items():
        key = f"{d.year}-{d.month:02d}"
        months[key] = months.get(key, ZERO) + v
    return IikoRevenueSyncReport(
        date_from=date_from,
        date_to=date_to,
        days=len(by_day),
        total=sum(by_day.values(), ZERO),
        result=result,
        months=dict(sorted(months.items())),
    )

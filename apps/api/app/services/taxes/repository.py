"""Загрузка фактов для движка налогов из БД.

Отделено от ``engine.py`` намеренно: математика остаётся чистой функцией и проверяется на
числах без базы, а здесь живут только запросы. Правила отбора, которые легко нарушить:

* вычет по взносам за работников берётся по ДАТЕ СПИСАНИЯ в окне [1 января .. срез] —
  кассовый принцип подп. 1 п. 3.1 ст. 346.21;
* авансы по УСН отбираются по ``for_year``, а НЕ по дате платежа: аванс, уплаченный
  28.04.2027, относится к налогу за 2026 год;
* НДФЛ не попадает в вычет — он отсечён списком ``DEDUCTIBLE_PAID_KINDS`` и, дополнительно,
  CHECK-констрейнтом на уровне БД.
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tax import (
    DEDUCTIBLE_PAID_KINDS,
    IikoRevenuePeriod,
    TaxPayment,
    TaxPeriodClose,
)
from app.services.taxes.engine import (
    YEAR_CONFIGS,
    ClaimPolicy,
    TaxComputationError,
    TaxInputs,
    YearConfig,
)

ZERO = Decimal("0")

DEFAULT_DEPARTMENT = "Foodmarket Тепло Черникова"


async def sum_revenue(
    session: AsyncSession,
    *,
    start: date,
    end: date,
    department: str = DEFAULT_DEPARTMENT,
) -> tuple[Decimal, set[tuple[int, int]]]:
    """Сумма выручки за период и множество покрытых месяцев ``(год, месяц)``.

    Смешивать гранулярности внутри месяца нельзя — сервис синхронизации это гарантирует,
    удаляя месячную строку при заливке подневных данных. Поэтому простая сумма корректна.
    """
    rows = (
        await session.execute(
            select(
                IikoRevenuePeriod.period_start,
                IikoRevenuePeriod.period_end,
                IikoRevenuePeriod.revenue_net,
            ).where(
                IikoRevenuePeriod.department == department,
                IikoRevenuePeriod.period_start >= start,
                IikoRevenuePeriod.period_end <= end,
            )
        )
    ).all()

    total = ZERO
    covered: set[tuple[int, int]] = set()
    for period_start, _period_end, revenue_net in rows:
        total += revenue_net
        covered.add((period_start.year, period_start.month))
    return total, covered


def expected_months(start: date, end: date) -> set[tuple[int, int]]:
    """Месяцы, которые обязаны быть покрыты базой на дату среза."""
    months: set[tuple[int, int]] = set()
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.add((year, month))
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return months


def month_bounds(year: int, month: int) -> tuple[date, date]:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def year_config(year: int) -> YearConfig:
    cfg = YEAR_CONFIGS.get(year)
    if cfg is None:
        raise TaxComputationError(
            f"Нет параметров налогового года {year}. Ставки и суммы взносов меняются "
            f"ежегодно — добавьте конфиг перед расчётом."
        )
    return cfg


async def _sum_payments(
    session: AsyncSession,
    *,
    kinds: tuple[str, ...],
    year: int | None = None,
    paid_from: date | None = None,
    paid_to: date | None = None,
) -> Decimal:
    stmt = select(func.coalesce(func.sum(TaxPayment.amount), 0)).where(
        TaxPayment.status == "paid",
        TaxPayment.kind.in_(kinds),
    )
    if year is not None:
        stmt = stmt.where(TaxPayment.for_year == year)
    if paid_from is not None:
        stmt = stmt.where(TaxPayment.paid_on >= paid_from)
    if paid_to is not None:
        stmt = stmt.where(TaxPayment.paid_on <= paid_to)
    return Decimal(str(await session.scalar(stmt) or 0))


async def _claimed_in_closed_periods(
    session: AsyncSession, *, year: int
) -> tuple[Decimal, Decimal]:
    """Сколько взносов «за себя» уже заявлено в зафиксированных периодах этого года."""
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(TaxPeriodClose.fixed_claimed), 0),
                func.coalesce(func.sum(TaxPeriodClose.extra_claimed), 0),
            ).where(TaxPeriodClose.year == year)
        )
    ).one()
    return Decimal(str(row[0])), Decimal(str(row[1]))


async def load_tax_inputs(
    session: AsyncSession,
    *,
    as_of: date,
    claim: ClaimPolicy | None = None,
    department: str = DEFAULT_DEPARTMENT,
) -> TaxInputs:
    """Собрать факты для расчёта на дату среза."""
    year = as_of.year
    cfg = year_config(year)
    jan1 = date(year, 1, 1)

    income_ytd, covered = await sum_revenue(
        session, start=jan1, end=as_of, department=department
    )
    expected = expected_months(jan1, as_of)

    employees_paid = await _sum_payments(
        session, kinds=DEDUCTIBLE_PAID_KINDS, paid_from=jan1, paid_to=as_of
    )
    # Взносы «за себя», уплаченные внутри периода. Нужны при основании 'cash'.
    # Окно то же [1 января .. срез]: платёж 25 июля в вычет за полугодие не попадает,
    # он относится к III кварталу — на этом сходятся обе позиции в споре.
    fixed_paid = await _sum_payments(
        session, kinds=("contrib_fixed",), paid_from=jan1, paid_to=as_of
    )
    extra_paid = await _sum_payments(
        session, kinds=("contrib_extra_1pct",), paid_from=jan1, paid_to=as_of
    )
    advances_paid = await _sum_payments(
        session, kinds=("usn_advance",), year=year, paid_to=as_of
    )
    contributions_paid = await _sum_payments(
        session,
        kinds=("contrib_employees", "contrib_injury", "contrib_fixed", "contrib_extra_1pct"),
        year=year,
    )
    fixed_used, extra_used = await _claimed_in_closed_periods(session, year=year)

    return TaxInputs(
        config=cfg,
        as_of=as_of,
        income_ytd=income_ytd,
        covered_months=len(covered & expected),
        expected_months=len(expected),
        employees_paid=employees_paid,
        fixed_paid=fixed_paid,
        extra_paid=extra_paid,
        fixed_used=fixed_used,
        extra_used=extra_used,
        advances_paid=advances_paid,
        contributions_paid=contributions_paid,
        claim=claim or ClaimPolicy.maximum(),
    )

"""Модуль «Налоги» как источник ОПиУ.

ПОЧЕМУ НЕ ИЗ ДДС, ХОТЯ СТАТЬЯ ЕСТЬ. Дата платежа и период налога — разные вещи, и для строки
отчёта нужен период. На проде за июль 2026 это видно буквально: 28-29 июля уплачены НДФЛ и
взносы с ``for_period='2026-06'`` — то есть за ИЮНЬ. Взяв кассу, мы записали бы июньский налог
в июльский расход, и так каждый месяц. Модуль налогов хранит период явно, поэтому источником
служит он.

ПОЧЕМУ УСН НЕ БЕРЁТСЯ ФАКТОМ ПЛАТЕЖА. Авансовый платёж 478 376 ₽, ушедший 29.07.2026, имеет
``for_period='h1'`` — это налог за ПЕРВОЕ ПОЛУГОДИЕ, а не за июль. Положить его в июль значило
бы показать месяц с полумиллионом налога, которого в нём нет, и обнулить остальные месяцы.
УСН — налог с выручки, и в помесячном отчёте он обязан начисляться от выручки месяца.
Пока зеркала выручки нет, строка честно отдаёт «нет данных»: это лучше, чем правдоподобная
неправда.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TaxPayment

#: Виды платежей, которые складываются в строку «Налоги с ЗП»: НДФЛ, взносы за работников,
#: травматизм. Это налоги на фонд оплаты труда, а не налог с дохода предпринимателя.
PAYROLL_TAX_KINDS = ("ndfl", "contrib_employees", "contrib_injury")

#: Виды, относящиеся к строке «Налоги» ниже EBITDA: УСН и фиксированные взносы ИП за себя.
INCOME_TAX_KINDS = ("usn_advance", "contrib_fixed", "contrib_extra_1pct")


def _month_code(month_start: date) -> str:
    """Код помесячного периода в модуле налогов: ``2026-07``."""
    return f"{month_start.year:04d}-{month_start.month:02d}"


async def payroll_taxes_for_month(session: AsyncSession, month_start: date) -> Decimal | None:
    """Налоги с ЗП, НАЧИСЛЕННЫЕ за месяц (не уплаченные в нём).

    Отбираем по ``for_period``, а не по дате списания: взносы за июль уходят в бюджет 28
    августа, но расходом являются июльскими. Плановые строки тоже берём — начисление
    существует независимо от того, ушли ли деньги; отменённые не берём.
    """
    rows = (
        (
            await session.execute(
                select(TaxPayment.amount).where(
                    TaxPayment.kind.in_(PAYROLL_TAX_KINDS),
                    TaxPayment.for_period == _month_code(month_start),
                    TaxPayment.for_year == month_start.year,
                    TaxPayment.status != "cancelled",
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    return sum(rows, Decimal("0.00"))


async def income_tax_for_month(
    session: AsyncSession, month_start: date
) -> tuple[Decimal | None, str | None]:
    """Налог с дохода за месяц. Возвращает (сумма, пояснение о качестве).

    Помесячного начисления УСН модуль не ведёт: он считает нарастающим итогом по кварталам и
    полугодиям. Поэтому месячной цифры здесь честно нет, и подменять её долей от квартального
    аванса нельзя — доли зависят от выручки месяца, а не от календаря.
    """
    quarter_rows = (
        await session.execute(
            select(TaxPayment.for_period, TaxPayment.amount).where(
                TaxPayment.kind.in_(INCOME_TAX_KINDS),
                TaxPayment.for_year == month_start.year,
                TaxPayment.status != "cancelled",
            )
        )
    ).all()
    if not quarter_rows:
        return None, None
    periods = sorted({period for period, _ in quarter_rows if period})
    total = sum((amount for _, amount in quarter_rows), Decimal("0.00"))
    return None, (
        f"За {month_start.year} год начислено {total:,.2f} ₽ по периодам {', '.join(periods)}. "
        "Помесячной разбивки модуль не ведёт — строка заполнится, когда появится выручка "
        "месяца и налог можно будет начислить от неё."
    ).replace(",", " ")

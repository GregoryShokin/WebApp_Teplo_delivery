"""Модуль «Налоги» как источник ОПиУ.

ПОЧЕМУ НЕ ИЗ ДДС, ХОТЯ СТАТЬЯ ЕСТЬ. Дата платежа и период налога — разные вещи, и строке
отчёта нужен период. На проде за июль 2026 это видно буквально: 28-29 июля уплачены НДФЛ и
взносы с ``for_period='2026-06'`` — то есть за ИЮНЬ. Взяв кассу, мы записали бы июньский налог
в июльский расход, и так каждый месяц.

ПОЧЕМУ УСН СЧИТАЕТСЯ ЗДЕСЬ, А НЕ БЕРЁТСЯ ПЛАТЕЖОМ. Авансовый платёж 478 376 ₽ от 29.07.2026
имеет ``for_period='h1'`` — это налог за ПОЛУГОДИЕ. Положить его в июль значило бы показать
месяц с полумиллионом налога, которого в нём нет, и обнулить остальные шесть.

Помесячная разбивка возможна и точна, потому что режим — УСН «доходы»: налог равен ставке от
выручки месяца, и ни первичка, ни расходы на него не влияют (подтверждено владельцем
03.08.2026). Поэтому здесь не приближение, а тот же расчёт, что у движка, применённый к
одному месяцу. Ставка и база берутся из ``YearConfig`` — они меняются законом ежегодно и живут
данными, а не константами; дублировать 0.06 в этом файле значило бы завести второе место,
которое однажды разойдётся с первым.

ЧЕГО ЗДЕСЬ НЕТ. Вычета взносов из налога: движок применяет его нарастающим итогом по периоду
и с лимитом 50 %, а разложить вычет по месяцам, не исказив ни один, нельзя. В ОПиУ строка
«Налоги» показывает НАЧИСЛЕННЫЙ налог месяца, а сэкономленное вычетом видно в модуле налогов
на своём экране — это разные вопросы: «во сколько обошёлся месяц» и «сколько платить в бюджет».
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IikoRevenuePeriod
from app.models.tax import TaxPayrollLedger
from app.services.taxes.engine import TaxComputationError, money
from app.services.taxes.repository import year_config


async def payroll_taxes_for_month(session: AsyncSession, month_start: date) -> Decimal | None:
    """Налоги с ЗП, НАЧИСЛЕННЫЕ за месяц: НДФЛ + взносы за работников + травматизм.

    ИСТОЧНИК — ОБОРОТКА БУХГАЛТЕРА (``tax_payroll_ledger``), а не платежи. Причина не в
    удобстве, а в том, что по платежам эту цифру посчитать НЕЛЬЗЯ: один и тот же факт лежит
    там дважды — строкой из банковской выписки и строкой из налогового уведомления. За
    январь 2026 взносы 13 595,93 ₽ дали бы 27 191,86 ₽, если сложить обе. Оборотка же
    хранит ровно одно начисление на сотрудника и месяц.

    НДФЛ включён по решению владельца 03.08.2026, и это парное решение: зарплата в отчёте
    берётся БЕЗ него, «на руки». Иначе НДФЛ посчитался бы дважды — внутри начисленной
    зарплаты и здесь.

    Оборотка покрывает только официальный контур — иного и не требуется: взносы и НДФЛ
    возникают ровно по нему.
    """
    row = (
        await session.execute(
            select(
                func.sum(TaxPayrollLedger.ndfl),
                func.sum(TaxPayrollLedger.contributions),
                func.sum(TaxPayrollLedger.injury),
            ).where(
                TaxPayrollLedger.year == month_start.year,
                TaxPayrollLedger.month == month_start.month,
            )
        )
    ).one()
    if all(value is None for value in row):
        return None
    return money(sum((value or Decimal("0.00") for value in row), Decimal("0.00")))


async def _month_revenue(session: AsyncSession, month_start: date, metric: str) -> Decimal | None:
    """Выручка месяца из зеркала iiko по метрике, принятой за налоговую базу."""
    column = (
        IikoRevenuePeriod.revenue_net
        if metric == "revenue_net"
        else IikoRevenuePeriod.revenue_gross
    )
    return await session.scalar(
        select(column).where(
            IikoRevenuePeriod.period_start == month_start,
            IikoRevenuePeriod.granularity == "month",
        )
    )


async def income_tax_for_month(
    session: AsyncSession, month_start: date
) -> tuple[Decimal | None, str | None]:
    """Налог с дохода за месяц: УСН от выручки плюс доля годовых взносов ИП «за себя».

    Возвращает (сумма, пояснение). Пояснение непусто, когда цифры нет, — чтобы владелец
    видел причину, а не пустую ячейку.
    """
    try:
        config = year_config(month_start.year)
    except TaxComputationError as error:
        return None, str(error)
    if config.regime != "usn_income":
        return None, (
            f"За {month_start.year} год действует режим «{config.regime}» — УСН к нему неприменим"
        )

    revenue = await _month_revenue(session, month_start, config.base_metric)
    if revenue is None:
        return None, (
            "Нет выручки месяца в зеркале iiko — налог считается от неё "
            f"(ставка {config.usn_rate * 100:.0f} %)"
        )

    usn = money(revenue * config.usn_rate)
    # Фиксированные взносы ИП «за себя» — годовая величина с одним сроком уплаты. В помесячном
    # отчёте она размазывается равными долями: обязанность возникает за год целиком, а не в
    # декабре, и класть все 57 390 ₽ в один месяц значило бы исказить и его, и одиннадцать
    # остальных. Допвзнос 1 % с превышения сюда не входит: он зависит от дохода нарастающим
    # итогом и появится в месяце, где порог реально перейдён, — отдельной задачей.
    fixed_share = money(config.fixed_contribution / Decimal("12"))
    return money(usn + fixed_share), None

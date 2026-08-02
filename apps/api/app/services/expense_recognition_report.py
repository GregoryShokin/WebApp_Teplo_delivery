"""Признанный расход по месяцам и статьям — мост из начислений в P&L.

ЗАЧЕМ. До сих пор у ``SupplierExpenseAccrual`` не было НИ ОДНОГО потребителя, кроме витрины,
которая его же и заводит. Признание работало, а выгрузить расход было некуда: отчёта о прибыли
в приложении нет, и любая ошибка признания (двойной расход, пропавший месяц) оставалась
невидимой — деньги-то сходились всегда. Аудит 02.08.2026 нашёл десяток таких мест ровно потому,
что искать пришлось глазами по коду, а не по расхождению в отчёте.

ЧТО СЧИТАЕТСЯ РАСХОДОМ МЕСЯЦА. Признанное начисление (``status='recognized'``) — и только оно:
``scheduled`` ещё не расход, ``cancelled`` уже не расход.

РАЗБИВКА ДЛИННЫХ ПЕРИОДОВ. Начисление хранит ОДНУ дату признания (``recognition_month``), и для
документа за квартал это последний месяц периода: акт на 36 000 ₽ за июль-сентябрь целиком
падал в сентябрь, а июль и август оставались пустыми. В учёте это неверно — услуга оказывалась
все три месяца. Здесь сумма раскладывается по календарным месяцам периода теми же долями, что
и у самоактов (``monthly_shares``): поровну, остаток от округления — последнему месяцу.

Менять ради этого схему начисления не понадобилось: ``recognition_month`` отвечает на вопрос
«когда расход подтверждён», а этот отчёт — на вопрос «к какому месяцу он относится». Это разные
вопросы, и смешивать их в одном поле было бы хуже, чем разложить при чтении.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Counterparty, DdsArticle, SupplierExpenseAccrual, SupplierInvoice
from app.services.subscription_accruals import SELF_BILLED_SOURCE, add_months, monthly_shares
from app.services.supplier_service_periods import money


@dataclass
class ExpenseCell:
    """Расход одного месяца по одной статье."""

    month: date
    article_id: uuid.UUID | None
    article_name: str
    amount: Decimal


@dataclass
class ExpenseReport:
    months: list[date] = field(default_factory=list)
    cells: list[ExpenseCell] = field(default_factory=list)
    total: Decimal = Decimal("0.00")
    # Расход, у которого нет статьи ДДС: в P&L его отнести некуда, и молчать об этом нельзя.
    unattributed: Decimal = Decimal("0.00")
    # Расход БЕЗ ПЕРВИЧКИ: признан самоактом (контрагент документа не присылал) либо строкой
    # ручного платежа, где документа нет вовсе. В управленческом P&L он полноправен — деньги
    # потрачены, услуга оказана. В налоговую базу УСН он идти НЕ МОЖЕТ: без первичного
    # документа инспекция такой расход снимет. Две цифры отвечают на разные вопросы —
    # «сколько мы потратили» и «сколько мы можем показать», — и складывать их в одну нельзя.
    without_primary: Decimal = Decimal("0.00")


def spread_over_months(
    amount: Decimal, start: date, end: date
) -> list[tuple[date, Decimal]]:
    """Разложить сумму периода по календарным месяцам.

    Доли равные, а не пропорциональные дням: так считает и помесячное признание из предоплаты
    (``monthly_shares``), и договор услуги. Разнобой в способе деления дал бы расхождение между
    механизмами на копейки в каждом месяце — при сверке с банком это ищется часами.
    """
    first = start.replace(day=1)
    last = end.replace(day=1)
    count = (last.year - first.year) * 12 + (last.month - first.month) + 1
    if count < 1:
        return []
    shares = monthly_shares(money(amount), count)
    return [(add_months(first, index), shares[index]) for index in range(count)]


async def build_expense_report(
    session: AsyncSession,
    *,
    date_from: date,
    date_to: date,
    counterparty_id: uuid.UUID | None = None,
    article_id: uuid.UUID | None = None,
) -> ExpenseReport:
    """Признанный расход по месяцам × статьям за интервал. Границы включительно, по месяцам.

    ``date_from``/``date_to`` — любые даты внутри крайних месяцев: приводим к первому числу.
    Начисление попадает в отчёт своими месяцами, пересекающимися с интервалом, — квартальный
    документ, начатый до ``date_from``, отдаёт только те месяцы, что внутри.
    """
    first = date_from.replace(day=1)
    last = date_to.replace(day=1)
    if last < first:
        return ExpenseReport()

    conditions = [SupplierExpenseAccrual.status == "recognized"]
    if counterparty_id is not None:
        conditions.append(SupplierExpenseAccrual.counterparty_id == counterparty_id)
    if article_id is not None:
        conditions.append(SupplierExpenseAccrual.article_id == article_id)
    rows = (
        await session.execute(
            select(SupplierExpenseAccrual, DdsArticle.name, SupplierInvoice.source)
            .outerjoin(DdsArticle, DdsArticle.id == SupplierExpenseAccrual.article_id)
            .outerjoin(SupplierInvoice, SupplierInvoice.id == SupplierExpenseAccrual.invoice_id)
            .where(*conditions)
        )
    ).all()

    buckets: dict[tuple[date, uuid.UUID | None], Decimal] = {}
    names: dict[uuid.UUID | None, str] = {None: "Без статьи"}
    unattributed = Decimal("0.00")
    without_primary = Decimal("0.00")
    for accrual, article_name, invoice_source in rows:
        # Первичка есть только у документа, присланного контрагентом. Самоакт (self_billed) мы
        # выписали себе сами, а у начисления по строке платежа документа нет вовсе.
        no_primary = invoice_source in (None, SELF_BILLED_SOURCE)
        if accrual.article_id is not None:
            names[accrual.article_id] = article_name or "Без названия"
        period_start = accrual.service_period_start
        period_end = accrual.service_period_end
        if period_start is None or period_end is None:
            # Периода нет — относим к месяцу признания: другого ответа у нас просто нет.
            month = accrual.recognition_month
            parts = [(month.replace(day=1), money(accrual.amount))] if month else []
        else:
            parts = spread_over_months(accrual.amount, period_start, period_end)
        for month, share in parts:
            if month < first or month > last:
                continue
            key = (month, accrual.article_id)
            buckets[key] = buckets.get(key, Decimal("0.00")) + share
            if accrual.article_id is None:
                unattributed += share
            if no_primary:
                without_primary += share

    months: list[date] = []
    cursor = first
    while cursor <= last:
        months.append(cursor)
        cursor = add_months(cursor, 1)

    cells = [
        ExpenseCell(
            month=month,
            article_id=key_article,
            article_name=names.get(key_article, "Без статьи"),
            amount=money(amount),
        )
        for (month, key_article), amount in sorted(
            buckets.items(), key=lambda item: (item[0][0], names.get(item[0][1], ""))
        )
    ]
    return ExpenseReport(
        months=months,
        cells=cells,
        total=money(sum((cell.amount for cell in cells), Decimal("0.00"))),
        unattributed=money(unattributed),
        without_primary=money(without_primary),
    )


async def counterparty_name(session: AsyncSession, counterparty_id: uuid.UUID) -> str | None:
    return await session.scalar(select(Counterparty.name).where(Counterparty.id == counterparty_id))

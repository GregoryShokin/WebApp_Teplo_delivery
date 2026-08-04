"""Чего месяц ещё ждёт: оплачено, а документа за ПЕРИОД УСЛУГИ нет.

ПЕРИОД БЕРЁТСЯ ИЗ ДЗ/КЗ, А НЕ ИЗ ДАТЫ ПЛАТЕЖА — в этом весь модуль. Раньше сигнал строился
из кассы месяца: «в июле ушло 68 000 ₽ по контекстной рекламе, документа нет». Звучит
правдоподобно и почти всегда неверно, потому что услуги оплачивают вперёд. Проверка данных
прода 03.08.2026 показала, что из СЕМИ июльских платежей услуговым контрагентам к июлю не
относится НИ ОДИН:

* АЙКО 4 260 + 16 430, Лема 3 700, ДОКСИНБОКС 15 580, Синапсис 13 000, О.О 68 000 — все за
  АВГУСТ (``supplier_prepayment.service_period_start = 2026-08-01``);
* «Назад в будущее» 80 455 — за ИЮНЬ.

А то единственное, чего июль действительно ждёт, было оплачено 29.06 и в кассу июля не
попадало вовсе: предоплата О.О на 48 000 ₽ с периодом 01.07–31.07, ``amount_settled = 0``.
Владелец назвал обе ошибки сразу — «выводы неверные, что платежи 22.07 это за июль, и при
этом информация в ДЗ/КЗ об этом есть».

ЧТО СЧИТАЕТСЯ ОЖИДАНИЕМ. Незакрытый остаток предоплаты (``amount > amount_settled``), период
услуги которой пересекается с месяцем. Незакрытый — потому что ``amount_settled`` растёт
ровно тогда, когда приезжает закрывающий документ; полностью зачтённая предоплата ничего не
ждёт, даже если признание по ней лежит в другой строке отчёта.

ПЕРИОД БЫВАЕТ НЕИЗВЕСТЕН, И ЭТО НЕ ОШИБКА ДАННЫХ. У Манго Телеком период не заполняется
принципиально: телефония по потреблению, сумма и период известны только из УПД
(``service_period_status='missing'``). Для таких платёж относится к месяцу, когда ушли
деньги, — другого ответа не существует. Именно эта ветка сохраняет депозитную модель Манго,
подтверждённую владельцем 01.08.2026: июль +10 000 ₽, УПД ещё нет.

У ЭТОЙ ВЕТКИ ЕСТЬ ОГРАНИЧИТЕЛЬ, И БЕЗ НЕГО ОНА ВРЁТ НА АРЕНДЕ. Дата платежа — не период, а
догадка о периоде, и догадка эта опровергается признанием. Арендодатель Виталий заплачен
30.07, периода у платежа нет — но июльская аренда по нему УЖЕ признана начислением из
договора аренды, все 50 000 ₽. Значит июль по этому контрагенту закрыт, и платёж относится к
какому угодно месяцу, только не к нему; строка от него не вырастет. Поэтому платёж без
периода становится ожиданием только у контрагента, чей месяц ещё НЕ признан.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CashflowTransaction,
    CounterpartyPayableProfile,
    SupplierExpenseAccrual,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services.expense_recognition_report import spread_over_months

#: Виды предоплат, которые вообще могут стать расходом ОПиУ. Заём собственнику и аванс
#: поставщику ТОВАРА — не услуги: первый не расход вовсе, второй закрывается накладной и
#: уходит в фудкост. Держать их здесь значило бы обещать документ, которого никто не ждёт.
EXPENSE_KINDS = frozenset({"subscription", "prepaid_bill", "ad"})

#: Период заполнен и ему можно верить. Остальные значения (``missing``, ``not_required``)
#: означают «периода нет» — тогда месяц определяется датой платежа.
PERIOD_READY = "ready"


@dataclass(slots=True)
class WaitingItem:
    """Один незакрытый платёж, относящийся к месяцу."""

    prepayment_id: uuid.UUID
    counterparty_id: uuid.UUID | None
    article_id: uuid.UUID | None
    amount: Decimal
    paid_on: date | None
    period_start: date | None
    period_end: date | None
    #: Период известен из ДЗ/КЗ, а не выведен из даты платежа.
    period_known: bool


@dataclass(slots=True)
class WaitingLayer:
    by_article: dict[uuid.UUID | None, Decimal] = field(default_factory=dict)
    items: list[WaitingItem] = field(default_factory=list)
    total: Decimal = Decimal("0.00")


def month_share(
    outstanding: Decimal,
    *,
    month_start: date,
    month_end: date,
    period_start: date | None,
    period_end: date | None,
    paid_on: date | None,
    counterparty_recognized: bool,
) -> Decimal | None:
    """Сколько из незакрытого остатка относится к месяцу. ``None`` — не относится ничего.

    Отдельной чистой функцией, потому что здесь живут ВСЕ четыре правила отнесения, и каждое
    из них уже давало неверный ответ на данных июля 2026. Проверять их через базу — значит
    проверять фикстуры, а не правила.
    """
    if period_start is not None and period_end is not None:
        # Период длиннее месяца делим теми же долями, что и признание, — иначе годовая
        # подписка целиком повисла бы на одном месяце и в отчёте, и в ожидании.
        share = sum(
            (
                value
                for part_month, value in spread_over_months(outstanding, period_start, period_end)
                if month_start <= part_month <= month_end
            ),
            Decimal("0.00"),
        )
        return share if share > 0 else None

    # Периода нет — единственная привязка к месяцу это дата денег. Без платежа привязать не к
    # чему вовсе: такую строку пропускаем, а не сваливаем в текущий месяц, иначе она будет
    # всплывать в каждом отчёте до скончания времён.
    if paid_on is None or not (month_start <= paid_on <= month_end):
        return None
    # Месяц по этому контрагенту уже признан — догадка по дате платежа опровергнута.
    if counterparty_recognized:
        return None
    return outstanding


@dataclass(slots=True)
class UnperiodedDocument:
    """Оплаченный закрывающий документ, по которому расход так и не признан."""

    invoice_id: uuid.UUID
    counterparty_id: uuid.UUID | None
    article_id: uuid.UUID | None
    number: str | None
    invoice_date: date
    amount: Decimal


@dataclass(slots=True)
class UnperiodedLayer:
    by_article: dict[uuid.UUID | None, Decimal] = field(default_factory=dict)
    items: list[UnperiodedDocument] = field(default_factory=list)
    total: Decimal = Decimal("0.00")


async def build_unperiodled_layer(
    session: AsyncSession, month_start: date, month_end: date
) -> UnperiodedLayer:
    """Документы месяца, у которых не заполнен период услуги, — расход по ним не признан.

    ПОЧЕМУ ЭТО ОТДЕЛЬНО ОТ «ЖДЁМ ДОКУМЕНТ». Там документа НЕТ и его приезд всё починит сам.
    Здесь документ УЖЕ ЛЕЖИТ в ДЗ/КЗ, оплачен, но без периода услуги признание по нему не
    срабатывает — и расход не появится никогда, пока человек не заполнит период. Молчать об
    этом хуже всего: строка выглядит законченной, а денег в ней нет.

    На июле 2026 это 27 685,59 ₽ по шести документам, и владелец увидел пропажу сразу —
    «в оплатах систем автоматизации нет АЙКО». Акт `070626-33538-лсп-акт` на 4 260 ₽ и счёт
    `060626-4260-лк` на 16 430 ₽ лежат оплаченными с 01.07, периода у обоих нет.

    МЕСЯЦ БЕРЁТСЯ ПО ДАТЕ ДОКУМЕНТА: закрывающие выписывают на конец периода, который они
    закрывают, так что 31.07 закрывает июль, а 30.06 — июнь. Это догадка, и потому величина
    в расход НЕ идёт: она только называется вслух.
    """
    profile_rows = await session.execute(
        select(
            CounterpartyPayableProfile.counterparty_id,
            CounterpartyPayableProfile.default_dds_article_id,
            CounterpartyPayableProfile.service_billing_mode,
            CounterpartyPayableProfile.settlement_contour,
        )
    )
    default_articles: dict[uuid.UUID, uuid.UUID] = {}
    service_counterparties: set[uuid.UUID] = set()
    for counterparty_id, article_id, billing_mode, contour in profile_rows:
        if article_id is not None:
            default_articles[counterparty_id] = article_id
        if billing_mode is not None or contour == "service":
            service_counterparties.add(counterparty_id)

    rows = (
        (
            await session.execute(
                select(SupplierInvoice).where(
                    SupplierInvoice.service_period_start.is_(None),
                    SupplierInvoice.payment_status == "paid",
                    SupplierInvoice.invoice_date >= month_start,
                    SupplierInvoice.invoice_date <= month_end,
                    ~select(SupplierExpenseAccrual.id)
                    .where(SupplierExpenseAccrual.invoice_id == SupplierInvoice.id)
                    .exists(),
                )
            )
        )
        .scalars()
        .all()
    )

    layer = UnperiodedLayer()
    for invoice in rows:
        if invoice.counterparty_id not in service_counterparties:
            continue
        article_id = invoice.dds_article_id or default_articles.get(invoice.counterparty_id)
        amount = invoice.amount or Decimal("0.00")
        if amount <= 0:
            continue
        layer.items.append(
            UnperiodedDocument(
                invoice_id=invoice.id,
                counterparty_id=invoice.counterparty_id,
                article_id=article_id,
                number=invoice.number,
                invoice_date=invoice.invoice_date,
                amount=amount,
            )
        )
        layer.by_article[article_id] = layer.by_article.get(article_id, Decimal("0.00")) + amount
        layer.total += amount
    return layer


async def build_waiting_layer(
    session: AsyncSession,
    month_start: date,
    month_end: date,
    *,
    recognized_counterparties: set[uuid.UUID] | None = None,
) -> WaitingLayer:
    """Незакрытые обязательства, относящиеся к месяцу.

    ``recognized_counterparties`` — у кого месяц уже закрыт признанием. Для них платёж без
    периода ожиданием не считается: сколько бы ни ушло денег, строка от них не вырастет.
    """
    recognized_counterparties = recognized_counterparties or set()
    default_articles = {
        counterparty_id: article_id
        for counterparty_id, article_id in (
            await session.execute(
                select(
                    CounterpartyPayableProfile.counterparty_id,
                    CounterpartyPayableProfile.default_dds_article_id,
                ).where(CounterpartyPayableProfile.default_dds_article_id.is_not(None))
            )
        ).all()
    }

    rows = (
        await session.execute(
            select(SupplierPrepayment, CashflowTransaction.operation_date)
            .outerjoin(
                CashflowTransaction,
                CashflowTransaction.id == SupplierPrepayment.cashflow_transaction_id,
            )
            .where(
                SupplierPrepayment.kind.in_(EXPENSE_KINDS),
                SupplierPrepayment.amount > SupplierPrepayment.amount_settled,
            )
        )
    ).all()

    layer = WaitingLayer()
    for prepayment, paid_on in rows:
        outstanding = (prepayment.amount or Decimal("0.00")) - (
            prepayment.amount_settled or Decimal("0.00")
        )
        if outstanding <= 0:
            continue

        article_id = prepayment.article_id or default_articles.get(prepayment.counterparty_id)
        period_known = (
            prepayment.service_period_status == PERIOD_READY
            and prepayment.service_period_start is not None
            and prepayment.service_period_end is not None
        )

        amount = month_share(
            outstanding,
            month_start=month_start,
            month_end=month_end,
            period_start=prepayment.service_period_start if period_known else None,
            period_end=prepayment.service_period_end if period_known else None,
            paid_on=paid_on,
            counterparty_recognized=prepayment.counterparty_id in recognized_counterparties,
        )
        if amount is None:
            continue

        layer.items.append(
            WaitingItem(
                prepayment_id=prepayment.id,
                counterparty_id=prepayment.counterparty_id,
                article_id=article_id,
                amount=amount,
                paid_on=paid_on,
                period_start=prepayment.service_period_start if period_known else None,
                period_end=prepayment.service_period_end if period_known else None,
                period_known=period_known,
            )
        )
        layer.by_article[article_id] = layer.by_article.get(article_id, Decimal("0.00")) + amount
        layer.total += amount

    return layer

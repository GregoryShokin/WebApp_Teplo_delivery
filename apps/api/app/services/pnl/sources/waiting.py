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

ОЖИДАНИЕ — НЕ ТРЕВОГА, ПОКА СРОК НЕ ВЫШЕЛ. До 24.09.2026 каждое ожидание становилось
предупреждением «закрывающего документа ещё нет». За сентябрь их было восемь строк на
334 459,84 ₽ — и ни одной настоящей: период ещё шёл; документы АЙКО, ЧОО, СПЕЦАВТО и
ЭкоЦентра уже лежали отложенными до 01.10; аренду никто и не ждал — её начислит договор.
Владелец видел «много всего, требующего внимания», и настоящая просрочка в этом списке
утонула бы. Поэтому у каждого ожидания есть СОСТОЯНИЕ (``waiting_state``), и тревогой
становится только просроченное; остальные — пометка на строке.
"""

from __future__ import annotations

import uuid
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CashflowTransaction,
    Counterparty,
    CounterpartyPayableProfile,
    SupplierExpenseAccrual,
    SupplierInvoice,
    SupplierPrepayment,
    UtilityAccount,
)
from app.services import clock
from app.services import counterparty_settlement_ledger as settlement
from app.services import supplier_prepayments as prepayments
from app.services.expense_recognition_report import spread_over_months

#: Виды предоплат, которые вообще могут стать расходом ОПиУ. Заём собственнику и аванс
#: поставщику ТОВАРА — не услуги: первый не расход вовсе, второй закрывается накладной и
#: уходит в фудкост. Держать их здесь значило бы обещать документ, которого никто не ждёт.
EXPENSE_KINDS = frozenset({"subscription", "prepaid_bill", "ad"})

#: Период заполнен и ему можно верить. Остальные значения (``missing``, ``not_required``)
#: означают «периода нет» — тогда месяц определяется датой платежа.
PERIOD_READY = "ready"

#: Состояния ожидания — от «делать нечего» к «пора звонить». Тревога — только последнее.
#:
#: Документ уже в системе и ждёт своей даты (``activation_status='pending'``) либо расход
#: начислит договор: ждать больше нечего, есть только день, когда всё встанет само.
STATE_DOCUMENT_PENDING = "document_pending"
#: Услуга ещё оказывается — документ за неё не выставят раньше конца периода.
STATE_PERIOD_RUNNING = "period_running"
#: Период кончился, но срок документа (``expected_by``) ещё не вышел.
STATE_AWAITING = "awaiting"
#: Срок вышел, документа нет. Единственное состояние, которое требует действия.
STATE_OVERDUE = "overdue"

#: Чем подтвердится ожидание, когда вступит в силу: бумагой контрагента или начислением
#: самой системы (аренда по договору, договор услуги, самоакт). Разница только в словах —
#: «документ получен» об аренде был бы неправдой: арендодатель документов не выставляет.
BASIS_DOCUMENT = "document"
BASIS_AGREEMENT = "agreement"
BASIS_ACCRUAL = "accrual"

#: Источники, чей документ завела сама система по договору — у них «начислится», а не
#: «документ получен». Коммунальный расчёт арендодателя (``utility``) сюда не входит: это
#: его бумага, пришедшая через бота.
AGREEMENT_SOURCES = frozenset({"lease", "self_billed"})


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
    counterparty_name: str | None = None
    #: Одно из ``STATE_*``. Считается на ``today`` — вчерашнее «ждём до 10.09» сегодня уже
    #: может быть просрочкой, поэтому состояние не хранится, а выводится при каждом отчёте.
    state: str = STATE_PERIOD_RUNNING
    #: Дата, которую называет состояние: когда вступит документ, до какого числа идёт
    #: период, до какого числа ждём, с какого числа просрочено.
    state_date: date | None = None
    overdue_days: int = 0
    #: Для ``document_pending`` — чем подтвердится: ``BASIS_*``.
    basis: str | None = None


@dataclass(slots=True)
class WaitingLayer:
    by_article: dict[uuid.UUID | None, Decimal] = field(default_factory=dict)
    items: list[WaitingItem] = field(default_factory=list)
    total: Decimal = Decimal("0.00")


def month_end_of(day: date) -> date:
    return day.replace(day=monthrange(day.year, day.month)[1])


def waiting_state(
    *,
    period_end: date,
    incoming_on: date | None,
    deadline: date,
    today: date,
) -> tuple[str, date, int]:
    """Состояние ожидания: ``(состояние, дата состояния, дней просрочки)``.

    Отдельной чистой функцией по той же причине, что и ``month_share``: правило проверяется
    на датах, а не на фикстурах.

    ``period_end`` — конец периода услуги, а для аванса без периода — конец месяца платежа:
    именно к этому месяцу ожидание и отнесено (``month_share``), и срок считается от него же.
    ``incoming_on`` — день, когда вступит отложенный документ или договорное начисление.

    ПОРЯДОК ПРОВЕРОК — ОТ СИЛЬНОГО ФАКТА К СЛАБОМУ. Документ, который уже лежит в системе,
    отвечает на вопрос полностью, даже пока период идёт: «вступит 01.10» говорит владельцу
    больше, чем «период идёт до 30.09». Срок проверяется последним: он нужен только там, где
    не известно ничего, кроме календаря.
    """
    if incoming_on is not None:
        return STATE_DOCUMENT_PENDING, incoming_on, 0
    if period_end >= today:
        return STATE_PERIOD_RUNNING, period_end, 0
    if today <= deadline:
        return STATE_AWAITING, deadline, 0
    return STATE_OVERDUE, deadline, (today - deadline).days


def state_label(item: WaitingItem) -> str:
    """Состояние словами — одна формулировка для строки отчёта, расшифровки и реестра."""
    on = f"{item.state_date:%d.%m}" if item.state_date else ""
    if item.state == STATE_DOCUMENT_PENDING:
        if item.basis == BASIS_DOCUMENT:
            return f"документ получен, вступит {on}"
        if item.basis == BASIS_AGREEMENT:
            return f"начислится по договору {on}"
        return f"расход начислится {on}"
    if item.state == STATE_PERIOD_RUNNING:
        return f"период идёт до {on}"
    if item.state == STATE_AWAITING:
        return f"ждём документ до {on}"
    return f"документ просрочен на {item.overdue_days} дн. (ждали до {on})"


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
    # Месяц по этой УСЛУГЕ уже признан — догадка по дате платежа опровергнута.
    #
    # ЩИТ РАБОТАЕТ ПО ПАРЕ «КОНТРАГЕНТ × СТРОКА», А НЕ ПО КОНТРАГЕНТУ ЦЕЛИКОМ, и это та же
    # ошибка, что жила в сверке непризнанного расхода. У арендодателя две разные услуги:
    # аренда начисляется по договору сама, коммуналку приносят бумагой. Признанная аренда
    # за июль 2026 глушила ожидание по коммуналке того же человека — 65 000 ₽ ушли 19.07,
    # документа нет, а отчёт показывал строку со статусом «всё в порядке». Сверка в соседнем
    # слое эти деньги тоже не видит: у платежа есть дебиторка, и он считается известным ДЗ/КЗ.
    # То есть щит по контрагенту был последним, что отделяло владельца от молчаливой потери.
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


@dataclass(slots=True)
class _Incoming:
    """Документ или начисление, которое уже в системе и вступит само."""

    article_id: uuid.UUID | None
    period_start: date
    period_end: date
    on: date
    basis: str


async def _incoming_by_counterparty(
    session: AsyncSession, default_articles: dict[uuid.UUID, uuid.UUID]
) -> dict[uuid.UUID, list[_Incoming]]:
    """Что уже лежит в системе и закроет ожидание без участия человека.

    Два источника, и оба нужны. Отложенный закрывающий (``activation_status='pending'``) —
    бумага контрагента, пришедшая раньше, чем кончилась услуга: АЙКО датирует акт первым
    числом оплаченного месяца, ЭкоЦентр — последним. Запланированное начисление
    (``status='scheduled'``) — то, что признается по окончании периода само: аренда по
    договору, договор услуги, документ с периодом. У документа с периодом есть и то и другое;
    у документа без периода — только первое, у договорной строки без документа — второе.

    Статья разрешается так же, как в признании (своя, иначе из карточки контрагента): у
    арендодателя аренда начисляется договором, а коммуналку приносят бумагой, и «Аренда
    09.2026» не вправе объявить полученным документ по коммуналке.
    """
    result: dict[uuid.UUID, list[_Incoming]] = {}

    documents = (
        await session.scalars(
            select(SupplierInvoice).where(
                SupplierInvoice.doc_kind == "closing",
                SupplierInvoice.activation_status == "pending",
                SupplierInvoice.payment_status != "void",
            )
        )
    ).all()
    for invoice in documents:
        on = prepayments._closing_effective_date(invoice)
        if on is None:
            # Без своей даты документ не откладывается — он вступает сразу, и pending без даты
            # означает сбой, а не ожидание. Выдавать его за «документ получен» нельзя.
            continue
        if invoice.service_period_status == PERIOD_READY and invoice.service_period_end:
            start = invoice.service_period_start or invoice.service_period_end
            end = invoice.service_period_end
        else:
            # Периода нет — закрывающие выписывают на конец закрываемого месяца, поэтому
            # месяц документа и есть его период. Та же догадка, что в ``build_unperiodled_layer``.
            start = invoice.invoice_date.replace(day=1)
            end = month_end_of(invoice.invoice_date)
        result.setdefault(invoice.counterparty_id, []).append(
            _Incoming(
                article_id=invoice.dds_article_id or default_articles.get(invoice.counterparty_id),
                period_start=start,
                period_end=end,
                on=on,
                basis=BASIS_AGREEMENT if invoice.source in AGREEMENT_SOURCES else BASIS_DOCUMENT,
            )
        )

    accruals = (
        await session.execute(
            select(SupplierExpenseAccrual, SupplierInvoice.source)
            .outerjoin(SupplierInvoice, SupplierInvoice.id == SupplierExpenseAccrual.invoice_id)
            .where(SupplierExpenseAccrual.status == "scheduled")
        )
    ).all()
    for accrual, source in accruals:
        if source is None:
            basis = BASIS_ACCRUAL
        elif source in AGREEMENT_SOURCES:
            basis = BASIS_AGREEMENT
        else:
            basis = BASIS_DOCUMENT
        result.setdefault(accrual.counterparty_id, []).append(
            _Incoming(
                article_id=accrual.article_id or default_articles.get(accrual.counterparty_id),
                period_start=accrual.service_period_start,
                period_end=accrual.service_period_end,
                # Признание забирает период строго ПОСЛЕ его окончания — расход за сентябрь
                # встанет 1 октября (``recognize_due_expenses``).
                on=accrual.service_period_end + timedelta(days=1),
                basis=basis,
            )
        )
    return result


def _match_incoming(
    candidates: list[_Incoming],
    *,
    article_id: uuid.UUID | None,
    period_start: date,
    period_end: date,
) -> tuple[date, str] | None:
    """Вступит ли что-то, что закроет ожидание, — и когда. ``None`` — нечему.

    Статья документа без статьи — подстановочная: у АЙКО её нет ни в акте, ни в карточке, а
    акт гасит именно этот аванс. Документ СО статьёй чужую статью не закрывает.
    """
    matched = [
        candidate
        for candidate in candidates
        if (candidate.article_id is None or candidate.article_id == article_id)
        and candidate.period_start <= period_end
        and candidate.period_end >= period_start
    ]
    if not matched:
        return None
    # Ожидание закрыто, когда вступит ПОСЛЕДНЕЕ из подходящих.
    on = max(candidate.on for candidate in matched)
    basis = (
        BASIS_DOCUMENT
        if any(candidate.basis == BASIS_DOCUMENT for candidate in matched)
        else matched[0].basis
    )
    return on, basis


async def _expected_days(
    session: AsyncSession,
) -> tuple[dict[uuid.UUID, int], dict[tuple[uuid.UUID, uuid.UUID], int]]:
    """До какого числа следующего месяца ждём документ: по контрагенту и по потоку коммуналки.

    Поток коммуналки точнее карточки: у арендодателя одна карточка, а расчёт по воде и
    электричеству приходит к своему числу (у Черниковой — к 20-му). Пусто везде — значит
    ``DEFAULT_CLOSING_DOC_DAY``, как и в сверке ДЗ/КЗ.
    """
    by_counterparty = {
        counterparty_id: day
        for counterparty_id, day in (
            await session.execute(
                select(
                    CounterpartyPayableProfile.counterparty_id,
                    CounterpartyPayableProfile.closing_doc_expected_day,
                ).where(CounterpartyPayableProfile.closing_doc_expected_day.is_not(None))
            )
        ).all()
    }
    by_stream: dict[tuple[uuid.UUID, uuid.UUID], int] = {}
    for counterparty_id, article_id, day in (
        await session.execute(
            select(
                UtilityAccount.counterparty_id,
                UtilityAccount.dds_article_id,
                UtilityAccount.expected_day,
            ).where(UtilityAccount.is_active.is_(True), UtilityAccount.expected_day.is_not(None))
        )
    ).all():
        # Несколько потоков одной статьи у одного контрагента (вода и газ) — ждём до
        # позднего: документ по паре закрыт, когда пришли оба.
        key = (counterparty_id, article_id)
        by_stream[key] = max(day, by_stream.get(key, day))
    return by_counterparty, by_stream


async def build_waiting_layer(
    session: AsyncSession,
    month_start: date,
    month_end: date,
    *,
    recognized_pairs: set[tuple[uuid.UUID, uuid.UUID | None]] | None = None,
    today: date | None = None,
) -> WaitingLayer:
    """Незакрытые обязательства, относящиеся к месяцу.

    ``recognized_pairs`` — пары «контрагент × статья», чей месяц уже закрыт признанием. Для
    них платёж без периода ожиданием не считается: сколько бы ни ушло денег, СВОЯ строка от
    них не вырастет. Ключ — пара, а не контрагент: у арендодателя аренда начисляется по
    договору сама, а коммуналку приносят бумагой, и признанная аренда не отвечает за то,
    приехал ли документ по коммуналке.

    ``today`` — день, на который считается состояние ожидания (по умолчанию сегодня по
    Москве). Какие суммы попали в месяц, от него не зависит — только их состояние.
    """
    recognized_pairs = recognized_pairs or set()
    today = today or clock.moscow_today()
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

    incoming = await _incoming_by_counterparty(session, default_articles)
    days_by_counterparty, days_by_stream = await _expected_days(session)
    names = dict(
        (
            await session.execute(
                select(Counterparty.id, Counterparty.name).where(
                    Counterparty.id.in_(
                        {prepayment.counterparty_id for prepayment, _paid_on in rows}
                    )
                )
            )
        ).all()
    )

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
            counterparty_recognized=(prepayment.counterparty_id, article_id) in recognized_pairs,
        )
        if amount is None:
            continue

        # Период ожидания: известный из ДЗ/КЗ, а без него — месяц платежа. Ровно к этому месяцу
        # ``month_share`` отнёс сумму, от его конца и считается срок документа.
        if period_known:
            period_start = prepayment.service_period_start
            period_end = prepayment.service_period_end
        else:
            # Без периода ``month_share`` пропускает только платёж с датой внутри месяца.
            anchor = paid_on or month_start
            period_start = anchor.replace(day=1)
            period_end = month_end_of(anchor)
        counterparty_id = prepayment.counterparty_id
        found = _match_incoming(
            incoming.get(counterparty_id, []),
            article_id=article_id,
            period_start=period_start,
            period_end=period_end,
        )
        expected_day = days_by_stream.get((counterparty_id, article_id))
        if expected_day is None:
            expected_day = days_by_counterparty.get(counterparty_id)
        state, state_date, overdue_days = waiting_state(
            period_end=period_end,
            incoming_on=found[0] if found else None,
            deadline=settlement.expected_by(period_end, expected_day),
            today=today,
        )

        layer.items.append(
            WaitingItem(
                prepayment_id=prepayment.id,
                counterparty_id=counterparty_id,
                article_id=article_id,
                amount=amount,
                paid_on=paid_on,
                period_start=prepayment.service_period_start if period_known else None,
                period_end=prepayment.service_period_end if period_known else None,
                period_known=period_known,
                counterparty_name=names.get(counterparty_id),
                state=state,
                state_date=state_date,
                overdue_days=overdue_days,
                basis=found[1] if found else None,
            )
        )
        layer.by_article[article_id] = layer.by_article.get(article_id, Decimal("0.00")) + amount
        layer.total += amount

    return layer

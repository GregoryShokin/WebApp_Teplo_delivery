"""Остатки ДЗ/КЗ с контрагентами НА ДАТУ — то, без чего баланс не собрать.

ЧЕМ ОТЛИЧАЕТСЯ ОТ ПЛИТКИ «ОСТАТКИ». Та отвечает на вопрос «сколько нам должны и сколько должны
мы ПРЯМО СЕЙЧАС»: берёт текущие статусы и текущие суммы гашений. Для баланса этого мало —
баланс собирается на конец месяца, и вопрос звучит иначе: «сколько было должно на 31 июля».
Ответить на него текущими статусами нельзя: документ, оплаченный 5 августа, сегодня закрыт,
а 31 июля был живой кредиторкой.

КАК СЧИТАЕМ. Обязательство существует на дату, если документ к ней уже вступил в силу
(правило 4 канона: поздняя из даты документа и конца подтверждённого периода услуги —
``_document_in_force``), и гасится теми аллокациями, чьё СОБЫТИЕ
произошло не позже даты. Дебиторка — зеркально: предоплата существует с даты своего денежного
факта и уменьшается гашениями до даты. У предоплаты по оплаченному счёту (``prepaid_bill``)
денежный факт — оплата самого счёта, а не момент, когда запись о дебиторке завели.

ЗАЧЁТ АВАНСОМ НЕ БОЛЬШЕ ДЕНЕГ. Документ гасится авансом в день вступления в силу, но закрыть
на дату он может только те деньги, что к ней уже ушли. Сколько их, говорит «фондирование»
предоплаты на дату: у аванса со своей проводкой — вся сумма с даты платежа, у ДЗ по счёту —
оплаченное по счёту к дате (счёт платят и частями — деньгами или зачтённым в него авансом).
Зачтённое сверх фондирования — живой долг:
он остаётся кредиторкой, пока деньги не придут. Так одно гашение описано одним правилом с обеих
сторон — и тогда, когда УПД датирован раньше оплаты счёта (ЭкоЦентр), и тогда, когда акт
пришёл между двумя частями оплаты.

ДАТА СОБЫТИЯ У ГАШЕНИЯ. У аллокации есть только ``created_at`` — когда строку записали в
систему. Для денежных гашений это не то же самое, что дата платежа: выписку разбирают через
день-два, а иногда через неделю. Поэтому дату берём из самого денежного факта:

* ``source_kind='cash'``/банк → дата проводки или банковской операции;
* ``source_kind='prepayment'`` → дата вступления в силу документа, который гасят:
  обязательство и его закрытие предоплатой возникают одним событием;
* ``source_kind='barter'`` → дата зачёта, а её у нас только по записи (``created_at``).

Последний случай — единственный, где дата приблизительна, и врать об этом не стоит: бартерные
зачёты оформляют в тот же день, но гарантии нет.

ЧЕГО ЗДЕСЬ НЕТ. Это остатки расчётов с контрагентами, а не баланс: ни сотрудников, ни налогов,
ни денег на счетах. Баланс соберётся из нескольких таких источников — этот закрывает свой.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import (
    BankOperation,
    CashflowTransaction,
    Counterparty,
    DdsArticle,
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services.banking.cashflow_classify import EXCLUDED_QUALITY
from app.services.supplier_prepayments import (
    BILL_PREPAYMENT_KIND,
    SUPPLIER_REFUND_ARTICLE_CODE,
    not_barter_money_return,
)
from app.services.supplier_service_periods import money

# Документы, которые вообще участвуют в расчётах: закрывающие финансовые обязательства.
# Счёт (bill) не долг по канону, справочный документ (informational) — тоже.
_DOC_CONDITIONS = (
    SupplierInvoice.direction == "payable",
    SupplierInvoice.doc_kind == "closing",
    SupplierInvoice.payment_status != "void",
    SupplierInvoice.informational.is_(False),
    SupplierInvoice.barter_role.is_(None),
)


def _msk_date(column):
    """Момент записи → календарный день ПО МОСКВЕ (как в остальном леджерном коде).

    Везде, где у события нет своей даты и ориентиром служит ``created_at`` — у предоплаты,
    гашения, документа, — день берётся только так. Голый ``date()`` считается в зоне сессии (на
    проде ``Etc/UTC``), а питоновские зеркала — сверка, очередь гашения, реестры — ведут день по
    Москве (``clock.moscow_date``): запись после полуночи МСК легла бы в них в разные сутки."""
    return func.date(func.timezone("Europe/Moscow", column))


@dataclass
class CounterpartyBalanceAsOf:
    counterparty_id: uuid.UUID
    counterparty_name: str
    receivable: Decimal  # нам должны (открытые предоплаты)
    payable: Decimal  # должны мы (неоплаченные закрывающие)

    @property
    def net(self) -> Decimal:
        return self.receivable - self.payable


@dataclass
class BalanceSheetAsOf:
    as_of: date
    rows: list[CounterpartyBalanceAsOf]
    receivable_total: Decimal
    payable_total: Decimal
    # Гашения, чью дату события установить не удалось (бартерные зачёты и аллокации без
    # денежного ключа): учтены по дате записи. Цифра нужна, чтобы расхождение баланса на
    # копейки не искали там, где его нет.
    approximate_settlements: Decimal


def _money_allocation_date():
    """Дата денежного гашения: проводка ДДС → операция выписки → дата записи строки."""
    return func.coalesce(
        CashflowTransaction.operation_date,
        BankOperation.operation_date,
        _msk_date(InvoicePaymentAllocation.created_at),
    )


def _document_in_force(fallback):
    """Дата, с которой закрывающий документ действует: поздняя из своей и конца периода услуги.

    Зеркало ``supplier_prepayments._closing_effective_date`` в SQL. Формула нужна в трёх местах
    расчёта (обязательство, дата гашения у КЗ и та же дата у ДЗ), и держать её копиями нельзя:
    стоит одной копии отстать — одно и то же гашение считается на разные даты у дебиторки и у
    кредиторки, а баланс перестаёт сходиться сам с собой.

    ``fallback`` — чем заменить отсутствующую дату документа (дата записи аллокации или самого
    документа, в зависимости от места вызова).

    Три ограничения повторяют питоновский оригинал слово в слово, и каждое куплено разбором:
    правило про ЗАКРЫВАЮЩИЙ документ («услуга оказана») — у счёта период значит обратное, за
    что платят ВПЕРЁД, и распространить на него правило значило бы прятать оплаченный аванс до
    конца оплаченного месяца; период считается только ``ready`` — недоверенный (``ambiguous``)
    не двигает ни обязательство, ни зачёт; без собственной даты документ не откладывается."""
    own = func.coalesce(SupplierInvoice.invoice_date, fallback)
    return case(
        (
            and_(
                SupplierInvoice.doc_kind == "closing",
                SupplierInvoice.invoice_date.is_not(None),
                SupplierInvoice.service_period_status == "ready",
                SupplierInvoice.service_period_end.is_not(None),
            ),
            func.greatest(SupplierInvoice.invoice_date, SupplierInvoice.service_period_end),
        ),
        else_=own,
    )


def _allocation_event_date():
    """Дата хозяйственного события аллокации — по ВИДУ гашения, а не по «что первое не NULL».

    Разделение по ``source_kind`` здесь принципиально. Дата документа верна только для
    гашения предоплатой: деньги ушли раньше, а обязательство и его закрытие авансом
    возникают одним событием — вступлением документа в силу. Для бартерного зачёта она НЕВЕРНА:
    зачёт оформляют месяцами позже прихода товара, и общий COALESCE, подхватывая
    ``invoice_date`` погашаемой накладной, делал условие ``event_date <= as_of``
    тождественно истинным — зачтённая товаром кредиторка не показывалась открытой НИ НА
    ОДНУ историческую дату, хотя реально висела с июля по сентябрь.

    ВСТУПЛЕНИЕ В СИЛУ, А НЕ ДАТА НА БУМАГЕ. Документ, выданный вперёд на месяц, начинает
    действовать по окончании услуги — так же, как его пропускает правило 4
    (``supplier_prepayments._closing_effective_date``). Пока здесь стояла голая
    ``invoice_date``, акт iiko от 01.08 за август списывал аванс первым числом: плитка
    «Остатки» держала дебиторку весь месяц (документ ждал в ``pending``), а баланс на
    середину августа показывал ноль. Два источника правды об одном контрагенте расходились
    на 20 690 ₽, и оба считали себя правыми.
    """
    document_in_force = _document_in_force(_msk_date(InvoicePaymentAllocation.created_at))
    return case(
        (
            InvoicePaymentAllocation.source_kind == "prepayment",
            # Сколько из зачтённого к дате покрыто деньгами, решает не дата зачёта, а
            # фондирование предоплаты (см. «ЗАЧЁТ АВАНСОМ НЕ БОЛЬШЕ ДЕНЕГ» в докстринге модуля):
            # одна дата на весь зачёт неверна, когда аванс оплачен частями.
            document_in_force,
        ),
        (
            InvoicePaymentAllocation.source_kind == "barter",
            # У зачёта денежного факта нет вовсе — остаётся дата записи, и это единственный
            # приблизительный случай во всём расчёте (см. approximate_settlements).
            _msk_date(InvoicePaymentAllocation.created_at),
        ),
        else_=_money_allocation_date(),
    )


# Проводка платежа, которым оплачен счёт: у доли разбора операции она стоит на самой аллокации,
# у прежних дверей — только через мост операции выписки.
_bill_payment_transaction = func.coalesce(
    InvoicePaymentAllocation.cashflow_transaction_id, BankOperation.cashflow_transaction_id
)
# Деньги платежа, уже ставшие дебиторкой правила 1 (предоплата со своей проводкой).
_rule1_receivable = (
    select(
        SupplierPrepayment.cashflow_transaction_id.label("transaction_id"),
        func.sum(SupplierPrepayment.amount).label("amount"),
    )
    .where(
        SupplierPrepayment.cashflow_transaction_id.is_not(None),
        SupplierPrepayment.kind != BILL_PREPAYMENT_KIND,
    )
    .group_by(SupplierPrepayment.cashflow_transaction_id)
    .subquery()
)
# Что уже пристроено из каждого платежа: все его аллокации по обоим ключам (зеркало
# ``payment_allocated_amount``) и отдельно — оплаты счетов без своей ДЗ, чьи деньги правило 1
# несёт по определению (``_transaction_carried_bill_allocations``).
_placed_allocation = aliased(InvoicePaymentAllocation)
_placed_operation = aliased(BankOperation)
_placed_invoice = aliased(SupplierInvoice)
_placed_bill_prepayment = aliased(SupplierPrepayment)
_placed_transaction = func.coalesce(
    _placed_allocation.cashflow_transaction_id, _placed_operation.cashflow_transaction_id
)
_transaction_placed = (
    select(
        _placed_transaction.label("transaction_id"),
        func.sum(_placed_allocation.amount).label("allocated"),
        func.sum(
            case(
                (
                    and_(
                        _placed_invoice.doc_kind == "bill",
                        ~select(_placed_bill_prepayment.id)
                        .where(
                            _placed_bill_prepayment.bill_invoice_id == _placed_invoice.id,
                            _placed_bill_prepayment.kind == BILL_PREPAYMENT_KIND,
                        )
                        .exists(),
                    ),
                    _placed_allocation.amount,
                ),
                else_=0,
            )
        ).label("unbooked_bills"),
    )
    .select_from(_placed_allocation)
    .join(_placed_invoice, _placed_invoice.id == _placed_allocation.invoice_id)
    .outerjoin(_placed_operation, _placed_operation.id == _placed_allocation.bank_operation_id)
    .where(_placed_allocation.source_kind != "prepayment", _placed_transaction.is_not(None))
    .group_by(_placed_transaction)
    .subquery()
)
# Сколько из аванса правила 1 — деньги счетов, а не свободный остаток платежа. Зеркало
# ``supplier_prepayments._rule1_bill_money``: аванс минус незанятое платежом, минус оплаты
# счетов, которые правило 1 несёт и так.
_rule1_transaction = aliased(CashflowTransaction)
_rule1_bill_money = (
    select(
        _rule1_receivable.c.transaction_id,
        func.greatest(
            _rule1_receivable.c.amount
            - func.greatest(
                _rule1_transaction.amount - func.coalesce(_transaction_placed.c.allocated, 0), 0
            )
            - func.coalesce(_transaction_placed.c.unbooked_bills, 0),
            0,
        ).label("amount"),
    )
    .join(_rule1_transaction, _rule1_transaction.id == _rule1_receivable.c.transaction_id)
    .outerjoin(
        _transaction_placed,
        _transaction_placed.c.transaction_id == _rule1_receivable.c.transaction_id,
    )
    .subquery()
)
_bill_prepayment = aliased(SupplierPrepayment)
# Какая доля платежа могла уже стать авансом правила 1: не больше денег счетов в этом авансе.
_rule1_share = case(
    (
        InvoicePaymentAllocation.source_kind.in_(("cash", "bank")),
        func.least(InvoicePaymentAllocation.amount, func.coalesce(_rule1_bill_money.c.amount, 0)),
    ),
    else_=0,
)


def _prepayment_money_on(prepayment, transaction, bill_paid_on=None):
    """День, с которого деньги предоплаты существуют: своя проводка → оплата счёта → дата записи.

    Одно правило на два места — дату денег самой дебиторки (``_prepayment_money_date``) и день,
    когда зачтённый в счёт аванс стал деньгами ДЗ по счёту (``_bill_payment_date``). Пока второе
    место знало только проводку, аванс без неё (входящий остаток ``create_opening_prepayment``)
    отдавал счёту деньги с даты счёта, а сам в балансе появлялся лишь датой записи: остаток,
    заведённый 20.07 и зачтённый в счёт от 01.07, с 01.07 по 19.07 уже был дебиторкой по счёту.

    ``bill_paid_on`` — первая оплата счёта у ДЗ ``prepaid_bill``; своей проводки у неё нет по
    конструкции (см. ``_prepayment_money_date``)."""
    dates = [transaction.operation_date]
    if bill_paid_on is not None:
        dates.append(bill_paid_on)
    dates.append(_msk_date(prepayment.created_at))
    return func.coalesce(*dates)


# Аванс, зачтённый в счёт (``settle_invoice_from_prepayment``), и его проводка.
_bill_source_prepayment = aliased(SupplierPrepayment)
_bill_source_money = aliased(CashflowTransaction)


def _bill_payment_date():
    """Когда гашение счёта стало деньгами ДЗ по счёту — той же датой, какой его видит источник.

    Зачёт аванса в счёт денег не двигает: аванс уменьшается датой вступления счёта в силу
    (``_allocation_event_date``), и ровно этой датой его деньги переезжают в ДЗ по счёту — иначе
    общая дебиторка на время между датами проседала бы или раздувалась на сумму зачёта. Но не
    раньше, чем деньги аванса появились в балансе: счёт, датированный до платежа, закрыт авансом
    с его дня, а входящий остаток — с дня записи.

    Звено «оплата счёта» из ``_prepayment_money_on`` здесь не нужно и невозможно: источником
    зачёта в счёт ДЗ по счёту быть не может (гард ``settle_invoice_from_prepayment``), а сама дата
    первой оплаты счёта считается из этих же строк — ссылка на неё замкнула бы расчёт на себя.
    """
    event_date = _allocation_event_date()
    return case(
        (
            InvoicePaymentAllocation.source_kind == "prepayment",
            func.greatest(
                event_date,
                func.coalesce(
                    _prepayment_money_on(_bill_source_prepayment, _bill_source_money), event_date
                ),
            ),
        ),
        else_=event_date,
    )


_bill_payment_rows = (
    select(
        InvoicePaymentAllocation.invoice_id.label("bill_invoice_id"),
        InvoicePaymentAllocation.amount.label("amount"),
        _bill_payment_date().label("paid_on"),
        _rule1_share.label("rule1_share"),
        func.sum(_rule1_share)
        .over(
            partition_by=InvoicePaymentAllocation.invoice_id,
            order_by=(InvoicePaymentAllocation.created_at, InvoicePaymentAllocation.id),
        )
        .label("rule1_running"),
        # Сколько денег счёта ДЗ по счёту не несёт — по данным самого чокпоинта.
        func.greatest(
            func.sum(InvoicePaymentAllocation.amount).over(
                partition_by=InvoicePaymentAllocation.invoice_id
            )
            - _bill_prepayment.amount,
            0,
        ).label("carried_elsewhere"),
    )
    .join(
        _bill_prepayment,
        and_(
            _bill_prepayment.bill_invoice_id == InvoicePaymentAllocation.invoice_id,
            _bill_prepayment.kind == BILL_PREPAYMENT_KIND,
        ),
    )
    .join(SupplierInvoice, SupplierInvoice.id == InvoicePaymentAllocation.invoice_id)
    .outerjoin(
        CashflowTransaction,
        CashflowTransaction.id == InvoicePaymentAllocation.cashflow_transaction_id,
    )
    .outerjoin(BankOperation, BankOperation.id == InvoicePaymentAllocation.bank_operation_id)
    .outerjoin(_rule1_bill_money, _rule1_bill_money.c.transaction_id == _bill_payment_transaction)
    .outerjoin(
        _bill_source_prepayment,
        _bill_source_prepayment.id == InvoicePaymentAllocation.prepayment_id,
    )
    .outerjoin(
        _bill_source_money,
        _bill_source_money.id == _bill_source_prepayment.cashflow_transaction_id,
    )
    # Гашения ВСЕХ видов, а не только денежные. Чокпоинт растит ДЗ по счёту на всё оплаченное
    # по нему (``amount − остаток``), в том числе на ручной зачёт аванса: деньги аванса переехали
    # в ДЗ счёта, а сам аванс этим зачётом закрыт. Пока здесь стояли одни денежные оплаты, счёт
    # 10 000 (4 000 деньгами + 6 000 зачётом аванса) давал ДЗ 4 000 при леджере 10 000, а акт на
    # 10 000, погашенный этой ДЗ, — фантомную кредиторку 6 000: зачёт аванса не был ничьими
    # деньгами — ни аванса (уже закрыт), ни счёта (не учтён).
    .subquery()
)
# Оплаты счетов — денежный факт ДЗ ``prepaid_bill`` — за вычетом денег, которые несёт аванс
# правила 1. Зеркало ``supplier_prepayments._bill_paid_already_receivable``: при штатном
# classify-then-match платёж сначала становится авансом правила 1, потом его привязывают к счёту,
# и чокпоинт заводит ДЗ счёта только на остаток. Взять эти деньги сюда целиком — и они встали бы
# в дебиторку дважды.
#
# ВЫЧИТАЕМ НЕ БОЛЬШЕ РАЗРЫВА «ОПЛАЧЕНО ПО СЧЁТУ − ДЗ ПО СЧЁТУ». Аванс правила 1 на той же проводке
# ещё не значит, что он несёт деньги СЧЁТА: платёж больше счёта сначала гасит счёт, и правило 1
# берёт только остаток. Вычесть его из денег счёта — и акт, закрытый ДЗ счёта, получил бы
# ложную кредиторку на сумму этого остатка. Долю денег счетов в авансе считаем как чокпоинт
# (``_rule1_bill_money``), а потолком остаётся его ответ — сумма ДЗ; разрыв распределяем по
# оплатам в порядке их записи, как он.
_bill_payments = (
    select(
        _bill_payment_rows.c.bill_invoice_id,
        _bill_payment_rows.c.paid_on,
        (
            _bill_payment_rows.c.amount
            - func.greatest(
                func.least(
                    _bill_payment_rows.c.rule1_running, _bill_payment_rows.c.carried_elsewhere
                )
                - func.least(
                    _bill_payment_rows.c.rule1_running - _bill_payment_rows.c.rule1_share,
                    _bill_payment_rows.c.carried_elsewhere,
                ),
                0,
            )
        ).label("amount"),
    )
).subquery()
_bill_first_payment = (
    select(
        _bill_payments.c.bill_invoice_id,
        # Первая оплата, в которой есть деньги самой ДЗ, а не аванса правила 1.
        func.min(case((_bill_payments.c.amount > 0, _bill_payments.c.paid_on))).label("paid_on"),
    )
    .group_by(_bill_payments.c.bill_invoice_id)
    .subquery()
)

# Дата, когда деньги предоплаты реально ушли. Носитель денежного факта у предоплат разный:
#
# * своя ДДС-проводка (аванс из выписки) — её дата;
# * ДЗ по оплаченному счёту (``prepaid_bill``) проводки не несёт ПО КОНСТРУКЦИИ: деньги уже
#   несёт аллокация счёта на реальный платёж, вторая ссылка задвоила бы расход
#   (``reconcile_bill_prepayment``). Её факт — оплаты самого счёта. Пока здесь стояла дата
#   записи, ДЗ возникала в день, когда её завёл чокпоинт, а не когда ушли деньги: аванс за
#   электричество, оплаченный 20.06 и заведённый позже 31.08, на 31.08 не существовал, и
#   июньский акт висел кредиторкой целиком — 95 402 ₽ вместо 30 402 ₽. Канон владельца от
#   17.07: дебиторку создают ОПЛАТЫ, а не записи о них;
# * входящий остаток — денежного факта нет вовсе, ориентир — день записи ПО МОСКВЕ, тот же, что
#   у зеркал: очередь гашения ``_settlement_order``, сверка, реестр платежей
#   (``clock.moscow_date``). Голый ``date()`` считается в зоне сессии — на проде UTC, — и займы
#   собственникам, записанные 03.08 в 00:42 МСК, появлялись в балансе вторым августа.
_prepayment_money_date = (
    select(
        SupplierPrepayment.id.label("prepayment_id"),
        _prepayment_money_on(
            SupplierPrepayment, CashflowTransaction, _bill_first_payment.c.paid_on
        ).label("money_date"),
        # Своей проводки нет, а у счёта есть оплаты: сумма на дату — оплаченное к дате.
        and_(
            CashflowTransaction.operation_date.is_(None),
            _bill_first_payment.c.bill_invoice_id.is_not(None),
        ).label("by_bill"),
    )
    .outerjoin(
        CashflowTransaction, CashflowTransaction.id == SupplierPrepayment.cashflow_transaction_id
    )
    .outerjoin(
        _bill_first_payment,
        _bill_first_payment.c.bill_invoice_id == SupplierPrepayment.bill_invoice_id,
    )
    .subquery()
)


async def build_balance_as_of(session: AsyncSession, *, as_of: date) -> BalanceSheetAsOf:
    """Остатки расчётов с контрагентами на конец указанной даты (включительно)."""
    event_date = _allocation_event_date()
    settled_by_invoice = (
        select(
            InvoicePaymentAllocation.invoice_id.label("invoice_id"),
            func.sum(InvoicePaymentAllocation.amount).label("settled"),
            func.sum(
                case(
                    # Приблизительной считается ровно та аллокация, у которой даты события НЕТ
                    # в данных: бартерный зачёт (денежного ключа нет по конструкции) и денежное
                    # гашение, потерявшее ссылку на проводку. Прежнее условие требовало NULL у
                    # ВСЕХ трёх источников сразу и потому давало ноль всегда — витрина
                    # честности молчала именно тогда, когда должна была говорить.
                    (
                        InvoicePaymentAllocation.source_kind == "barter",
                        InvoicePaymentAllocation.amount,
                    ),
                    (
                        (InvoicePaymentAllocation.source_kind != "prepayment")
                        & CashflowTransaction.operation_date.is_(None)
                        & BankOperation.operation_date.is_(None),
                        InvoicePaymentAllocation.amount,
                    ),
                    else_=0,
                )
            ).label("approximate"),
        )
        .outerjoin(
            CashflowTransaction,
            CashflowTransaction.id == InvoicePaymentAllocation.cashflow_transaction_id,
        )
        .outerjoin(BankOperation, BankOperation.id == InvoicePaymentAllocation.bank_operation_id)
        .outerjoin(SupplierInvoice, SupplierInvoice.id == InvoicePaymentAllocation.invoice_id)
        .where(event_date <= as_of)
        .group_by(InvoicePaymentAllocation.invoice_id)
        .subquery()
    )

    payable_rows = (
        await session.execute(
            select(
                SupplierInvoice.counterparty_id,
                func.sum(
                    func.greatest(
                        SupplierInvoice.amount - func.coalesce(settled_by_invoice.c.settled, 0),
                        0,
                    )
                ),
                func.sum(func.coalesce(settled_by_invoice.c.approximate, 0)),
            )
            .outerjoin(settled_by_invoice, settled_by_invoice.c.invoice_id == SupplierInvoice.id)
            .where(
                *_DOC_CONDITIONS,
                # Правило 4 канона: документ становится обязательством, когда услуга по нему
                # оказана — по поздней из двух дат, своей и конца периода услуги (то же, что
                # ``supplier_prepayments._closing_effective_date``). Акт, выданный вперёд на
                # месяц, до конца периода обязательством не является: иначе у поставщика,
                # оплаченного авансом, в середине месяца одновременно висели бы кредиторка по
                # неоказанной услуге и списанный аванс. Документ без даты считаем действующим
                # с момента записи — других ориентиров нет.
                _document_in_force(_msk_date(SupplierInvoice.created_at)) <= as_of,
            )
            .group_by(SupplierInvoice.counterparty_id)
        )
    ).all()

    # Дебиторка: предоплата живёт с даты своего денежного факта и гасится аллокациями до даты.
    in_force = _document_in_force(_msk_date(InvoicePaymentAllocation.created_at))
    settled_by_prepayment = (
        select(
            InvoicePaymentAllocation.prepayment_id.label("prepayment_id"),
            func.sum(InvoicePaymentAllocation.amount).label("settled"),
            # Та часть, что гасит документы, которые кредиторка выше считает долгом.
            func.sum(
                case((and_(*_DOC_CONDITIONS), InvoicePaymentAllocation.amount), else_=0)
            ).label("settled_debts"),
        )
        .outerjoin(SupplierInvoice, SupplierInvoice.id == InvoicePaymentAllocation.invoice_id)
        .where(InvoicePaymentAllocation.prepayment_id.is_not(None), in_force <= as_of)
        .group_by(InvoicePaymentAllocation.prepayment_id)
        .subquery()
    )
    # Сколько денег у предоплаты к дате. ДЗ по счёту растёт с каждой его оплатой: на дату между
    # двумя частями существует только первая, и одна дата на всю сумму показала бы вторую часть
    # дебиторкой раньше, чем она ушла со счёта.
    bill_paid_by_date = (
        select(
            _bill_payments.c.bill_invoice_id,
            func.sum(_bill_payments.c.amount).label("paid"),
        )
        .where(_bill_payments.c.paid_on <= as_of)
        .group_by(_bill_payments.c.bill_invoice_id)
        .subquery()
    )
    funded = case(
        (
            _prepayment_money_date.c.by_bill,
            func.least(SupplierPrepayment.amount, func.coalesce(bill_paid_by_date.c.paid, 0)),
        ),
        (_prepayment_money_date.c.money_date <= as_of, SupplierPrepayment.amount),
        else_=0,
    )

    # Зачтено больше, чем к дате пришло денег: разница — живой долг, возвращаем её кредиторке.
    shortfall_rows = (
        await session.execute(
            select(
                SupplierPrepayment.counterparty_id,
                func.sum(func.greatest(settled_by_prepayment.c.settled_debts - funded, 0)),
            )
            .join(
                settled_by_prepayment,
                settled_by_prepayment.c.prepayment_id == SupplierPrepayment.id,
            )
            .join(
                _prepayment_money_date,
                _prepayment_money_date.c.prepayment_id == SupplierPrepayment.id,
            )
            .outerjoin(
                bill_paid_by_date,
                bill_paid_by_date.c.bill_invoice_id == SupplierPrepayment.bill_invoice_id,
            )
            .group_by(SupplierPrepayment.counterparty_id)
        )
    ).all()

    receivable_rows = (
        await session.execute(
            select(
                SupplierPrepayment.counterparty_id,
                func.sum(
                    func.greatest(funded - func.coalesce(settled_by_prepayment.c.settled, 0), 0)
                ),
            )
            .join(
                _prepayment_money_date,
                _prepayment_money_date.c.prepayment_id == SupplierPrepayment.id,
            )
            .outerjoin(
                bill_paid_by_date,
                bill_paid_by_date.c.bill_invoice_id == SupplierPrepayment.bill_invoice_id,
            )
            .outerjoin(
                settled_by_prepayment,
                settled_by_prepayment.c.prepayment_id == SupplierPrepayment.id,
            )
            # Фильтра по СТАТУСУ здесь нет намеренно: ``refunded`` — состояние на сегодня, а
            # предоплата, возвращённая 10 августа, на 31 июля была живой дебиторкой. Возврат
            # вычитается ниже по дате своей проводки — это и есть «состояние на дату» вместо
            # «текущего статуса», ради чего весь модуль и написан.
            #
            # ОДНО ИСКЛЮЧЕНИЕ — закрытие БЕЗ аллокации. Дозачётные остатки и ручные коррекции
            # ставят ``settled`` прямым присвоением (``scripts/writeoff_pre_accounting``,
            # разбор исторических расчётов), не создавая строки гашения. Гашение считается по
            # аллокациям, поэтому такая предоплата висела бы открытой дебиторкой НА ЛЮБУЮ дату,
            # хотя закрыта решением человека. На проде это 38 479 ₽ одной строкой.
            # Дату такого закрытия несёт ``settled_on`` (миграция 0273); у строк, закрытых до
            # её появления, фолбэк — день создания. Фолбэк ЗАНИЖАЕТ срок жизни дебиторки, но
            # для единственной прод-строки разница в два дня и на срез не влияет.
            .where(_prepayment_money_date.c.money_date <= as_of)
            .where(
                or_(
                    SupplierPrepayment.status != "settled",
                    # ВНИМАНИЕ: проверять надо наличие аллокаций ВООБЩЕ, а не в подзапросе
                    # ``settled_by_prepayment`` — тот отфильтрован по дате, и «гашений не было
                    # НА ЭТУ ДАТУ» читалось бы как «гашений нет вовсе». Предоплата, зачтённая
                    # позже среза, тогда выбрасывалась бы из дебиторки задним числом — то есть
                    # ровно наоборот тому, ради чего расчёт на дату написан.
                    select(InvoicePaymentAllocation.id)
                    .where(InvoicePaymentAllocation.prepayment_id == SupplierPrepayment.id)
                    .exists(),
                    func.coalesce(
                        SupplierPrepayment.settled_on,
                        _msk_date(SupplierPrepayment.created_at),
                    )
                    > as_of,
                )
            )
            .group_by(SupplierPrepayment.counterparty_id)
        )
    ).all()

    # Возврат денег от поставщика гасит дебиторку РОСТОМ amount_settled, без строки
    # InvoicePaymentAllocation (см. refund_counterparty_prepayments) — поэтому подзапросом
    # гашений он не виден вовсе, и ДЗ была бы завышена на всю сумму возвратов, на любую дату.
    # Считаем возвраты по контрагенту: связи «возврат ↔ конкретная предоплата» в данных нет,
    # FIFO-гашение её не сохраняет. По сумме это верно, по разрезу предоплат — нет, но разрез
    # предоплат в балансе и не нужен.
    refund_rows = (
        await session.execute(
            select(
                CashflowTransaction.counterparty_id,
                func.sum(CashflowTransaction.amount),
            )
            .join(DdsArticle, DdsArticle.id == CashflowTransaction.article_id)
            .where(
                CashflowTransaction.direction == "in",
                CashflowTransaction.counterparty_id.is_not(None),
                CashflowTransaction.operation_date <= as_of,
                CashflowTransaction.quality_status != EXCLUDED_QUALITY,
                DdsArticle.code == SUPPLIER_REFUND_ARTICLE_CODE,
                not_barter_money_return(),
            )
            .group_by(CashflowTransaction.counterparty_id)
        )
    ).all()
    refunds_by_cp = {row[0]: money(row[1]) for row in refund_rows}

    payable_by_cp = {row[0]: money(row[1]) for row in payable_rows}
    for cp_id, shortfall in shortfall_rows:
        if shortfall:
            payable_by_cp[cp_id] = payable_by_cp.get(cp_id, Decimal("0.00")) + money(shortfall)
    approximate = money(sum((row[2] or Decimal("0") for row in payable_rows), Decimal("0")))
    receivable_by_cp = {
        row[0]: max(money(row[1]) - refunds_by_cp.get(row[0], Decimal("0.00")), Decimal("0.00"))
        for row in receivable_rows
    }
    ids = set(payable_by_cp) | set(receivable_by_cp)
    if not ids:
        return BalanceSheetAsOf(
            as_of=as_of,
            rows=[],
            receivable_total=Decimal("0.00"),
            payable_total=Decimal("0.00"),
            approximate_settlements=Decimal("0.00"),
        )
    names = dict(
        (
            await session.execute(
                select(Counterparty.id, Counterparty.name).where(Counterparty.id.in_(ids))
            )
        ).all()
    )
    rows = [
        CounterpartyBalanceAsOf(
            counterparty_id=cp_id,
            counterparty_name=names.get(cp_id, "—"),
            receivable=receivable_by_cp.get(cp_id, Decimal("0.00")),
            payable=payable_by_cp.get(cp_id, Decimal("0.00")),
        )
        for cp_id in ids
    ]
    rows = [row for row in rows if row.receivable or row.payable]
    rows.sort(key=lambda row: row.counterparty_name)
    return BalanceSheetAsOf(
        as_of=as_of,
        rows=rows,
        receivable_total=money(sum((row.receivable for row in rows), Decimal("0.00"))),
        payable_total=money(sum((row.payable for row in rows), Decimal("0.00"))),
        approximate_settlements=approximate,
    )

"""Сверка расчётов с контрагентом: платежи и закрывающие документы одной хронологией.

ЗАЧЕМ ОТДЕЛЬНЫЙ РЕЕСТР. На странице ДЗ/КЗ уже есть реестр платежей и реестр документов, но
они живут раздельно и по всем контрагентам сразу. Вопрос «мы заплатили — закрыли ли это
документом?» ими не отвечается: надо открыть два экрана, отфильтровать каждый и сверить
глазами. Именно так и потерялся УПД Микроэля за май — разрыв нашли через два месяца.

МОДЕЛЬ. Строка — либо платёж (деньги ушли), либо закрывающий документ (обязательство
подтверждено). Бегущий остаток считается как «деньги минус документы»:

    платёж   → +сумма   (заплатили вперёд, ждём документ → дебиторка растёт)
    документ → −сумма   (документ пришёл → дебиторка гаснет, а без платежа уходит в минус)

Итог хронологии сходится с плиткой «Остатки» той же страницы: там дебиторка — это открытые
предоплаты (amount − settled), кредиторка — неоплаченный остаток активных закрывающих. Пара
«платёж 1000 + УПД 1000» даёт 0 в обеих моделях, одинокий платёж — дебиторку, одинокий
документ — кредиторку. Параллельного учёта здесь нет: связи берутся из тех же аллокаций.

ЧТО СЧИТАЕТСЯ РАЗРЫВОМ. У каждого платежа есть непокрытая часть:

    uncovered = сумма платежа − прямые гашения документов − закрытое его предоплатой

То есть сколько денег ещё не подтверждено закрывающим документом. Пока период услуги не
кончился, это норма (заплатили за август — УПД будет в сентябре). Разрывом это становится
после срока: конец периода + ``closing_doc_expected_day`` из карточки (NULL = 1-е число).
Так контрагент, который честно присылает УПД 5-го, не краснеет каждое 1-е число.

БАРТЕР СЮДА НЕ ВХОДИТ. Бартерные документы гасятся товаром через BarterReturnLine, а не
аллокациями: их сумма в «деньги минус документы» уводила бы остаток в минус на весь заём.
У бартерных контрагентов свой контур и своя вкладка — здесь только денежные расчёты.
"""

from __future__ import annotations

import uuid
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CashflowTransaction,
    Counterparty,
    CounterpartyPayableProfile,
    CounterpartyServiceAgreement,
    DdsArticle,
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierPrepayment,
    Wallet,
)

# Статусы предоплат, которые ещё держат дебиторку (те же, что в плитке «Остатки»).
OPEN_PREPAYMENT_STATUSES = ("open", "partially_settled")

# Контуры расчётов. goods — закрывающие приходят накладными и гасятся складским контуром
# автоматически; service — гашение ручное, и именно там копится ложная дебиторка.
CONTOUR_GOODS = "goods"
CONTOUR_SERVICE = "service"

# Подтип услуги, от которого ежемесячных закрывающих не ждут вовсе: разовые работы (ремонт,
# юрист, типография). Тишина по ним — норма, а не разрыв.
BILLING_MODE_ONE_OFF = "one_off"
# Долг известен из договора и начисляется сам; закрывающих документов от контрагента не ждут.
BILLING_MODE_AGREEMENT = "agreement"


def money(value: Decimal | int | float | None) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _clamp(value: Decimal) -> Decimal:
    return value if value > 0 else Decimal("0")


def period_of(start: date | None, end: date | None, fallback: date) -> tuple[date, date]:
    """Период платежа: явный из предоплаты, иначе календарный месяц операции.

    Месяц операции — сознательный фолбэк, а не заглушка: услуга без размеченного периода
    почти всегда оплачивается в своём же месяце, и разрыв по ней всё равно надо увидеть.
    Ошибку в периоде человек правит на строке — период предоплаты редактируемый.
    """
    if start is not None and end is not None:
        return start, end
    first = fallback.replace(day=1)
    return first, fallback.replace(day=monthrange(fallback.year, fallback.month)[1])


def expected_by(period_end: date, expected_day: int | None) -> date:
    """Последний день, когда закрывающий документ ещё ожидается; после него — разрыв.

    Отсчёт от конца периода услуги, а не от даты платежа: документ выставляют по факту
    оказания, поэтому за август ждать его в августе бессмысленно.

    NULL = конец периода, то есть строка краснеет ровно 1-го числа следующего месяца
    (требование владельца). Заданное число — день, в который контрагент обычно присылает
    документ: в этот день ещё ждём, краснеет со следующего. Иначе «присылает 5-го» и
    «просрочил 5-го» означали бы одно и то же, и поле теряло бы смысл.
    """
    if expected_day is None:
        return period_end
    year = period_end.year + (1 if period_end.month == 12 else 0)
    month = 1 if period_end.month == 12 else period_end.month + 1
    return date(year, month, min(expected_day, monthrange(year, month)[1]))


@dataclass
class LedgerRow:
    """Строка сверки: платёж или закрывающий документ."""

    kind: str  # 'payment' | 'document'
    id: uuid.UUID
    row_date: date
    amount: Decimal
    title: str
    subtitle: str | None
    period_start: date | None
    period_end: date | None
    # Только для платежей: сколько денег ещё не подтверждено документом.
    uncovered: Decimal = Decimal("0")
    # ok — платёж закрыт документом либо документ пришёл; waiting — срок ещё не наступил;
    # overdue — срок прошёл, документа нет.
    status: str = "ok"
    expected_by: date | None = None
    days_overdue: int = 0
    # Ссылки на встречные строки: чем платёж закрыт / чем документ оплачен.
    links: list[str] = field(default_factory=list)
    # Бегущий остаток ПОСЛЕ этой строки: >0 дебиторка, <0 кредиторка.
    balance_after: Decimal = Decimal("0")
    prepayment_id: uuid.UUID | None = None
    # Документ создан нами (source='self_billed'), а не прислан контрагентом.
    self_billed: bool = False
    # Двигает ли строка бегущий остаток. Платёж — всегда. Документ — только если он в силе
    # (не будущий) и не информационный: у обоих обязательства ещё/уже нет. Пока остаток
    # считался по всем документам подряд, два арендных УПД от 31.08 по 50 000 ₽ уводили
    # сверку на 100 000 ₽ от плитки «Остатки», которая их честно не видела.
    binds: bool = True


@dataclass
class LedgerMonth:
    """Подытог месяца: сколько заплатили, на сколько получили документов, что зависло."""

    month: str  # YYYY-MM
    paid: Decimal
    documented: Decimal
    gap: Decimal
    has_overdue: bool


@dataclass
class Ledger:
    counterparty_id: uuid.UUID
    counterparty_name: str
    contour: str
    contour_manual: bool
    closing_doc_expected_day: int | None
    opening_balance: Decimal
    closing_balance: Decimal
    total_paid: Decimal
    total_documented: Decimal
    overdue_amount: Decimal
    # Сколько признано нами, без первички от контрагента: эти расходы есть в P&L,
    # но в налоговую базу УСН не идут.
    self_billed_amount: Decimal
    rows: list[LedgerRow]
    months: list[LedgerMonth]
    has_barter: bool


async def documents_not_expected(
    session: AsyncSession, counterparty_id: uuid.UUID, *, today: date
) -> bool:
    """От контрагента ежемесячных закрывающих документов не ждут — значит и разрыва нет.

    Сводка разрывов держится на ожидании: «заплатили — где документ?». Там, где документа не
    будет по договорённости, вечно красная строка не информация, а шум, из-за которого
    перестают смотреть на всю сводку.

    Два случая, и оба — решение человека, а не догадка:

    * ``service_billing_mode='one_off'`` — разовые работы (ремонт, юрист, типография). Тишина
      между заказами — норма;
    * действующий договор услуги с ``documents_mode='informal'`` — документов не будет вовсе,
      вместо них долг считает ночное начисление, и оно же закрывает платёж.
    """
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == counterparty_id
        )
    )
    if profile is not None and profile.service_billing_mode in (
        BILLING_MODE_ONE_OFF,
        BILLING_MODE_AGREEMENT,
    ):
        return True
    informal = await session.scalar(
        select(CounterpartyServiceAgreement.id).where(
            CounterpartyServiceAgreement.counterparty_id == counterparty_id,
            CounterpartyServiceAgreement.documents_mode == "informal",
            CounterpartyServiceAgreement.started_on <= today,
            or_(
                CounterpartyServiceAgreement.ended_on.is_(None),
                CounterpartyServiceAgreement.ended_on >= today,
            ),
        )
    )
    return informal is not None


async def resolve_contour(session: AsyncSession, counterparty_id: uuid.UUID) -> tuple[str, bool]:
    """Контур контрагента и признак «выбран вручную».

    Явное значение в карточке сильнее факта: контрагент может возить товар и параллельно
    оказывать услуги, и тогда владелец решает, в каком контуре его контролировать.
    """
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == counterparty_id
        )
    )
    if profile is not None and profile.settlement_contour in (CONTOUR_GOODS, CONTOUR_SERVICE):
        return profile.settlement_contour, True
    warehouse = await session.scalar(
        select(func.count(SupplierInvoice.id)).where(
            SupplierInvoice.counterparty_id == counterparty_id,
            SupplierInvoice.operational_scope == "warehouse",
            SupplierInvoice.payment_status != "void",
        )
    )
    return (CONTOUR_GOODS if (warehouse or 0) > 0 else CONTOUR_SERVICE), False


async def _payment_rows(
    session: AsyncSession, counterparty_id: uuid.UUID
) -> list[tuple[date, uuid.UUID, Decimal, str, str | None, SupplierPrepayment | None, Decimal]]:
    """Денежные строки: ДДС-проводки контрагенту + входящие остатки без движения денег.

    Возвращает кортежи (дата, id, сумма, заголовок, подпись, предоплата, прямые гашения),
    чтобы вызывающий собрал из них строки сверки, не повторяя запросы.
    """
    tx_rows = (
        await session.execute(
            select(CashflowTransaction, Wallet.name, DdsArticle.name)
            .join(Wallet, Wallet.id == CashflowTransaction.wallet_id)
            .outerjoin(DdsArticle, DdsArticle.id == CashflowTransaction.article_id)
            .where(
                CashflowTransaction.direction == "out",
                CashflowTransaction.counterparty_id == counterparty_id,
                CashflowTransaction.quality_status != "excluded",
            )
            .order_by(CashflowTransaction.operation_date, CashflowTransaction.created_at)
        )
    ).all()

    tx_ids = [row[0].id for row in tx_rows]
    # Мультисплит: у долей общий source_id банк-операции. «Ничьи» аллокации операции вешаем
    # на первую по времени долю — иначе одно гашение попало бы в каждую строку и покрытие
    # задвоилось бы (та же оговорка, что в реестре платежей).
    bank_op_to_tx: dict[uuid.UUID, uuid.UUID] = {}
    for row in tx_rows:
        tx = row[0]
        if tx.source_kind == "bank_operation" and tx.source_id is not None:
            bank_op_to_tx.setdefault(tx.source_id, tx.id)

    direct_allocs: list[InvoicePaymentAllocation] = []
    if tx_ids or bank_op_to_tx:
        conditions = []
        if tx_ids:
            conditions.append(InvoicePaymentAllocation.cashflow_transaction_id.in_(tx_ids))
        if bank_op_to_tx:
            conditions.append(
                InvoicePaymentAllocation.bank_operation_id.in_(list(bank_op_to_tx.keys()))
            )
        direct_allocs = list(
            (await session.scalars(select(InvoicePaymentAllocation).where(or_(*conditions)))).all()
        )

    prepayments = list(
        (
            await session.scalars(
                select(SupplierPrepayment).where(
                    SupplierPrepayment.counterparty_id == counterparty_id
                )
            )
        ).all()
    )
    prepayment_by_tx = {
        sp.cashflow_transaction_id: sp for sp in prepayments if sp.cashflow_transaction_id
    }

    out: list[
        tuple[date, uuid.UUID, Decimal, str, str | None, SupplierPrepayment | None, Decimal]
    ] = []
    for tx, wallet_name, article_name in tx_rows:
        allocs = [
            a
            for a in direct_allocs
            if a.cashflow_transaction_id == tx.id
            or (
                a.cashflow_transaction_id is None
                and a.bank_operation_id is not None
                and bank_op_to_tx.get(a.bank_operation_id) == tx.id
            )
        ]
        direct_total = sum((money(a.amount) for a in allocs), Decimal("0"))
        out.append(
            (
                tx.operation_date,
                tx.id,
                money(tx.amount),
                wallet_name or "Платёж",
                article_name or (tx.payment_purpose or tx.comment),
                prepayment_by_tx.get(tx.id),
                direct_total,
            )
        )

    # Входящие остатки (opening) — деньги ушли до внедрения системы. Оплаченные счета
    # (kind='prepaid_bill') сюда не берём: их денежный факт уже пришёл своей ДДС-проводкой,
    # иначе один платёж дал бы в сверке две строки и задвоил бы остаток.
    for sp in prepayments:
        if sp.cashflow_transaction_id is None and sp.kind != "prepaid_bill":
            out.append(
                (
                    sp.created_at.date(),
                    sp.id,
                    money(sp.amount),
                    "Входящий остаток",
                    sp.note,
                    sp,
                    Decimal("0"),
                )
            )
    return out


async def build_ledger(
    session: AsyncSession,
    counterparty_id: uuid.UUID,
    *,
    today: date,
    date_from: date | None = None,
    date_to: date | None = None,
) -> Ledger:
    """Хронология расчётов с контрагентом с бегущим остатком и разметкой разрывов."""
    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None:
        raise ValueError("Контрагент не найден")
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == counterparty_id
        )
    )
    expected_day = profile.closing_doc_expected_day if profile else None
    contour, contour_manual = await resolve_contour(session, counterparty_id)

    payments = await _payment_rows(session, counterparty_id)

    doc_rows = (
        await session.execute(
            select(SupplierInvoice)
            .where(
                SupplierInvoice.counterparty_id == counterparty_id,
                SupplierInvoice.direction == "payable",
                SupplierInvoice.doc_kind == "closing",
                SupplierInvoice.payment_status != "void",
            )
            .order_by(SupplierInvoice.invoice_date, SupplierInvoice.created_at)
        )
    ).all()
    documents = [row[0] for row in doc_rows]
    has_barter = any(doc.barter_role is not None for doc in documents)
    documents = [doc for doc in documents if doc.barter_role is None]

    doc_allocs: dict[uuid.UUID, list[InvoicePaymentAllocation]] = {}
    alloc_rows: list[InvoicePaymentAllocation] = []
    if documents:
        alloc_rows = list(
            (
                await session.scalars(
                    select(InvoicePaymentAllocation).where(
                        InvoicePaymentAllocation.invoice_id.in_([d.id for d in documents])
                    )
                )
            ).all()
        )
        for alloc in alloc_rows:
            doc_allocs.setdefault(alloc.invoice_id, []).append(alloc)

    # Деньги, закрывшие документ этого контрагента ПОМИМО его собственных проводок. Так бывает,
    # когда платёж разнесён на другого контрагента: на проде накладную ИП Скачковой закрыла
    # проводка, помеченная «ООО ТОРА» (либо ошибка разметки в ДДС, либо платёж за третье лицо).
    # Без этой строки документ в хронологии есть, а денег под ним нет — остаток занижается ровно
    # на её сумму и расходится с плиткой. Показываем платёж честно и подписываем, чей он.
    seen_tx = {row[1] for row in payments}
    external_rows: list[tuple[date, uuid.UUID, Decimal, str]] = []
    external_ids = [
        alloc.cashflow_transaction_id
        for alloc in alloc_rows
        if alloc.prepayment_id is None
        and alloc.cashflow_transaction_id is not None
        and alloc.cashflow_transaction_id not in seen_tx
    ]
    if external_ids:
        tx_meta = {
            row[0]: (row[1], row[2])
            for row in (
                await session.execute(
                    select(
                        CashflowTransaction.id,
                        CashflowTransaction.operation_date,
                        Counterparty.name,
                    )
                    .outerjoin(Counterparty, Counterparty.id == CashflowTransaction.counterparty_id)
                    .where(CashflowTransaction.id.in_(external_ids))
                )
            ).all()
        }
        for alloc in alloc_rows:
            tx_id = alloc.cashflow_transaction_id
            if alloc.prepayment_id is not None or tx_id is None or tx_id in seen_tx:
                continue
            op_date, payer = tx_meta.get(tx_id, (alloc.created_at.date(), None))
            external_rows.append(
                (
                    op_date,
                    alloc.id,
                    money(alloc.amount),
                    f"Оплата по проводке «{payer}»" if payer else "Оплата вне карточки",
                )
            )

    rows: list[LedgerRow] = []
    for row_date, row_id, amount, title, subtitle, prepayment, direct_total in payments:
        start, end = period_of(
            prepayment.service_period_start if prepayment else None,
            prepayment.service_period_end if prepayment else None,
            row_date,
        )
        settled_by_prepayment = money(prepayment.amount_settled) if prepayment else Decimal("0")
        uncovered = _clamp(amount - direct_total - settled_by_prepayment)
        deadline = expected_by(end, expected_day)
        if uncovered <= 0:
            status = "ok"
        elif today <= deadline:
            status = "waiting"
        else:
            status = "overdue"
        rows.append(
            LedgerRow(
                kind="payment",
                id=row_id,
                row_date=row_date,
                amount=amount,
                title=title,
                subtitle=subtitle,
                period_start=start,
                period_end=end,
                uncovered=uncovered,
                status=status,
                expected_by=deadline,
                days_overdue=(today - deadline).days if status == "overdue" else 0,
                prepayment_id=prepayment.id if prepayment else None,
            )
        )

    for row_date, row_id, amount, title in external_rows:
        rows.append(
            LedgerRow(
                kind="payment",
                id=row_id,
                row_date=row_date,
                amount=amount,
                title=title,
                subtitle="деньги ушли по проводке другого контрагента — проверьте разметку в ДДС",
                period_start=None,
                period_end=None,
                uncovered=Decimal("0"),
                status="ok",
            )
        )

    for doc in documents:
        paid = sum((money(a.amount) for a in doc_allocs.get(doc.id, [])), Decimal("0"))
        remainder = _clamp(money(doc.amount) - paid)
        doc_date = doc.invoice_date or doc.created_at.date()
        start, end = period_of(doc.service_period_start, doc.service_period_end, doc_date)
        # Самоакт — наш внутренний документ по абонентскому платежу: контрагент его не
        # выставлял. Называть его «УПД №…» нельзя ни в коем случае: человек решит, что
        # первичка есть, и учтёт расход в налоговой базе, которой он не принадлежит.
        self_billed = doc.source == "self_billed"
        binds = doc.activation_status == "active" and not doc.informational
        rows.append(
            LedgerRow(
                kind="document",
                id=doc.id,
                row_date=doc_date,
                amount=money(doc.amount),
                title=(
                    "Признано без документа"
                    if self_billed
                    else (f"УПД № {doc.number}" if doc.number else "Закрывающий документ")
                ),
                subtitle=(
                    "первички нет — в налоговые расходы не идёт"
                    if self_billed
                    else "расход уже начислен по договору — документ справочный"
                    if doc.informational
                    else "не оплачен"
                    if remainder > 0 and doc.activation_status == "active"
                    else ("вступает в силу позже" if doc.activation_status != "active" else None)
                ),
                period_start=start,
                period_end=end,
                uncovered=remainder if binds else Decimal("0"),
                status="ok",
                links=[f"оплачено {paid}"] if paid > 0 else [],
                self_billed=self_billed,
                binds=binds,
            )
        )

    rows.sort(key=lambda r: (r.row_date, 0 if r.kind == "payment" else 1))

    opening = Decimal("0")
    running = Decimal("0")
    visible: list[LedgerRow] = []
    for row in rows:
        if row.kind == "payment":
            delta = row.amount
        elif row.binds:
            delta = -row.amount
        else:
            # Будущий или информационный документ виден в хронологии, но остаток не двигает —
            # обязательства по нему нет. Иначе сверка расходится с плиткой «Остатки».
            delta = Decimal("0")
        running += delta
        if date_from is not None and row.row_date < date_from:
            opening = running
            continue
        if date_to is not None and row.row_date > date_to:
            continue
        row.balance_after = running
        visible.append(row)

    months: dict[str, LedgerMonth] = {}
    for row in visible:
        # Группируем по дате СОБЫТИЯ, а не по периоду услуги: строки идут хронологией, и
        # ключ по периоду заставлял один месяц появляться дважды — квартальный платёж
        # относится к апрелю, а признание за апрель стоит на три месяца ниже.
        key = row.row_date.strftime("%Y-%m")
        bucket = months.setdefault(
            key,
            LedgerMonth(
                month=key,
                paid=Decimal("0"),
                documented=Decimal("0"),
                gap=Decimal("0"),
                has_overdue=False,
            ),
        )
        if row.kind == "payment":
            bucket.paid += row.amount
            bucket.gap += row.uncovered
            bucket.has_overdue = bucket.has_overdue or row.status == "overdue"
        else:
            bucket.documented += row.amount

    visible.sort(key=lambda r: (r.row_date, 0 if r.kind == "payment" else 1), reverse=True)
    return Ledger(
        counterparty_id=counterparty_id,
        counterparty_name=counterparty.name,
        contour=contour,
        contour_manual=contour_manual,
        closing_doc_expected_day=expected_day,
        opening_balance=opening,
        closing_balance=running,
        total_paid=sum((r.amount for r in visible if r.kind == "payment"), Decimal("0")),
        total_documented=sum((r.amount for r in visible if r.kind == "document"), Decimal("0")),
        overdue_amount=sum(
            (r.uncovered for r in visible if r.kind == "payment" and r.status == "overdue"),
            Decimal("0"),
        ),
        self_billed_amount=sum(
            (r.amount for r in visible if r.kind == "document" and r.self_billed),
            Decimal("0"),
        ),
        rows=visible,
        months=sorted(months.values(), key=lambda m: m.month, reverse=True),
        has_barter=has_barter,
    )


@dataclass
class GapRow:
    """Строка сводки разрывов: контрагент × период услуги."""

    counterparty_id: uuid.UUID
    counterparty_name: str
    period_start: date
    period_end: date
    amount: Decimal
    expected_by: date
    days_overdue: int
    payments: int
    last_payment_date: date


async def list_gaps(
    session: AsyncSession,
    *,
    today: date,
    include_goods: bool = False,
) -> list[GapRow]:
    """Платежи без закрывающего документа, у которых срок ожидания уже прошёл.

    Группируем по контрагенту и периоду услуги: два платежа за один месяц — одна строка,
    иначе сводка превращается в тот же реестр платежей, из которого владельцу и приходилось
    вылавливать разрывы вручную. Товарные контрагенты по умолчанию не показываются — их
    закрывающие приходят накладными и гасятся складским контуром сами.
    """
    cp_ids = set(
        (
            await session.scalars(
                select(SupplierPrepayment.counterparty_id).where(
                    SupplierPrepayment.status.in_(OPEN_PREPAYMENT_STATUSES),
                    SupplierPrepayment.amount > SupplierPrepayment.amount_settled,
                )
            )
        ).all()
    )
    gaps: list[GapRow] = []
    for cp_id in cp_ids:
        contour, _manual = await resolve_contour(session, cp_id)
        if contour == CONTOUR_GOODS and not include_goods:
            continue
        if await documents_not_expected(session, cp_id, today=today):
            continue
        ledger = await build_ledger(session, cp_id, today=today)
        buckets: dict[tuple[date, date], GapRow] = {}
        for row in ledger.rows:
            if row.kind != "payment" or row.status != "overdue":
                continue
            key = (row.period_start or row.row_date, row.period_end or row.row_date)
            existing = buckets.get(key)
            if existing is None:
                buckets[key] = GapRow(
                    counterparty_id=cp_id,
                    counterparty_name=ledger.counterparty_name,
                    period_start=key[0],
                    period_end=key[1],
                    amount=row.uncovered,
                    expected_by=row.expected_by or key[1],
                    days_overdue=row.days_overdue,
                    payments=1,
                    last_payment_date=row.row_date,
                )
            else:
                existing.amount += row.uncovered
                existing.payments += 1
                existing.days_overdue = max(existing.days_overdue, row.days_overdue)
                existing.last_payment_date = max(existing.last_payment_date, row.row_date)
        gaps.extend(buckets.values())
    gaps.sort(key=lambda g: (-g.days_overdue, -float(g.amount)))
    return gaps

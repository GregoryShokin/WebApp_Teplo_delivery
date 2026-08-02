"""Предоплаты поставщикам (дебиторка): мы платим вперёд — поставщик должен привезти.

Создание предоплаты = реальный расход денег (out-CashflowTransaction, source_kind=
'supplier_prepayment'), обычно с кошелька «Сейф». Гашение приходящих payable-накладных —
через InvoicePaymentAllocation(source_kind='prepayment'), которая денег НЕ двигает (они
ушли при создании предоплаты). Отдельный учёт от кредиторки и товарного бартера.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    BankOperation,
    CashflowTransaction,
    Counterparty,
    CounterpartyPayableProfile,
    DdsArticle,
    ExpenseDraftLine,
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierPrepayment,
    Wallet,
    invoice_binds_settlement,
)
from app.services.banking.cashflow_classify import EXCLUDED_QUALITY
from app.services.counterparty_matching import (
    _invoice_remaining,
    _recompute_status,
    payment_allocated_amount,
)
from app.services.counterparty_payments import CounterpartyPaymentError, _money

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

PREPAYMENT_ARTICLE_CODE = "advance_to_supplier"
# Приходная статья «Возврат переплаты от поставщиков» — возврат гасит открытые предоплаты.
SUPPLIER_REFUND_ARTICLE_CODE = "vozvrat_pereplaty_ot_postavschikov"
OPEN_PREPAYMENT_STATUSES = ("open", "partially_settled")
# Открытая кредиторка контрагента = неоплаченный остаток АКТИВНЫХ закрывающих документов.
UNPAID_INVOICE_STATUSES = ("unpaid", "partially_paid")
# Автоматический взаимозачёт ДЗ/КЗ разрешён только финансовому документообороту (УПД/акты
# услуг). Товарные накладные operational_scope='warehouse' всегда связываются с авансом
# вручную: одинаковый поставщик может привезти несколько независимых поставок, и FIFO без
# решения оператора способен зачесть аванс не в ту накладную.
AUTO_SETTLEMENT_OPERATIONAL_SCOPE = "finance"

# Контуры, которые сами закрывают свои документы деньгами той же проводки. Правило 1 к ним
# неприменимо: чек кассы (``kassa_cheque``) заводит собственную накладную и оплачивает её в том
# же потоке — товар получен на месте, поставщик нам ничего не должен. Пустили бы их сюда —
# каждая переразметка кассового чека рождала бы фантомную дебиторку на «Местный закуп».
SELF_SETTLING_SOURCE_KINDS = frozenset({"kassa_cheque", "kassa_cheque_refund"})

# Наличные контуры, распорядившиеся деньгами АДРЕСНО: оплата конкретного счёта/накладной,
# выплата целевого резерва (гасит обязательство своего договора аренды), явная предоплата.
# Правило 1 раскладывает деньги вслепую по FIFO и, вмешавшись в ручной разбор такой проводки,
# сняло бы адресную привязку и перевесило платёж на посторонний документ.
DEDICATED_MONEY_SOURCE_KINDS = frozenset(
    {
        "counterparty_payment",
        "supplier_prepayment",
        "safe_payout",
        "kassa_target_payout",
        "kassa_payout",
        "kassa_payin",
    }
)

# Целевые авансы под конкретную поставку (kind='goods') гасятся ЯВНО — когда придёт накладная
# именно этой поставки (settle_invoice_from_prepayment). Их НЕЛЬЗЯ авто-гасить FIFO любой
# приходящей накладной: иначе аванс под недопоставленный заказ молча «съест» посторонний
# счёт/УПД. Остальные виды (subscription/ad/rent/other) — это «деньги у поставщика» (баланс
# рекламного кабинета/подписки), который закрывающие документы правомерно списывают по FIFO.
# deposit (залог за помещение) тоже целевой: залог лежит у арендодателя до конца аренды, и
# ежемесячная арендная накладная не должна его съедать — иначе залог исчез бы из дебиторки, а
# КЗ выглядела бы оплаченной без движения денег. Возврат/зачёт залога — явным действием.
EARMARKED_PREPAYMENT_KINDS = frozenset({"goods", "deposit"})

# ДЗ из оплаты счёта (doc_kind='bill'). Канон владельца 17.07: «Сам по себе счёт ничего не делает…
# Оплата счёта уходит в дебиторскую задолженность». Счёт — не обязательство (в КЗ не входит), но
# его ОПЛАТА — предоплата поставщику: деньги ушли, закрывающего документа ещё нет. Закрывающий
# УПД/акт затем гасит эту ДЗ по правилу 2 (auto_settle). Kind НЕ входит в
# EARMARKED_PREPAYMENT_KINDS, иначе закрывающий документ не смог бы её авто-погасить и мы
# получили бы фантомную КЗ (блокер-2).
BILL_PREPAYMENT_KIND = "prepaid_bill"

# Вид, которым правило 1 метит СВОЮ дебиторку. Всё остальное на той же проводке — чужая запись
# (целевой аванс кассы/Сейфа, ДЗ оплаченного счёта от чокпоинта): её нельзя ни пересобрать, ни
# удалить, ни пустить на FIFO-гашение.
RULE1_PREPAYMENT_KIND = "subscription"
FOREIGN_PREPAYMENT_KINDS = EARMARKED_PREPAYMENT_KINDS | {BILL_PREPAYMENT_KIND}


def _prepayment_untouched(prepayment: SupplierPrepayment) -> bool:
    """Предоплата ещё не начала гаситься: можно безопасно чинить/сносить реквизиты."""
    return prepayment.status == "open" and _money(prepayment.amount_settled) == 0


def _consume_prepayment(
    prepayment: SupplierPrepayment, amount: Decimal, *, full_status: str
) -> None:
    """Списать часть остатка предоплаты и перевести статус.

    Единственное место арифметики amount_settled и порога «исчерпана» — ручное гашение,
    авто-гашение и возврат обязаны считать одинаково, иначе копии разъезжаются
    (см. исторический edge 'refunded со стейл amount_settled<amount')."""
    prepayment.amount_settled = _money(prepayment.amount_settled) + amount
    prepayment.status = (
        full_status
        if prepayment.amount_settled >= _money(prepayment.amount)
        else "partially_settled"
    )


async def _allocate_invoice_from_prepayment(
    session: AsyncSession,
    *,
    invoice: SupplierInvoice,
    prepayment: SupplierPrepayment,
    amount: Decimal,
    actor_user_id: uuid.UUID | None,
) -> None:
    """Аллокация «накладная ← предоплата» (денег не двигает) + списание остатка."""
    session.add(
        InvoicePaymentAllocation(
            invoice_id=invoice.id,
            source_kind="prepayment",
            prepayment_id=prepayment.id,
            amount=amount,
            created_by_user_id=actor_user_id,
        )
    )
    _consume_prepayment(prepayment, amount, full_status="settled")
    await session.flush()


async def create_supplier_prepayment(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    wallet_id: uuid.UUID,
    amount: Decimal,
    operation_date: date,
    article_id: uuid.UUID | None = None,
    kind: str = "goods",
    note: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> SupplierPrepayment:
    """Завести предоплату поставщику: реальный расход с кошелька + запись дебиторки.

    Деньги уходят сразу (out-CashflowTransaction), возникает остаток «поставщик нам
    должен». Накладные гасятся против него позже через settle_invoice_from_prepayment.
    """
    cp = await session.get(Counterparty, counterparty_id)
    if cp is None:
        raise CounterpartyPaymentError("Контрагент не найден")

    amt = _money(amount)
    if amt <= 0:
        raise CounterpartyPaymentError("Сумма предоплаты должна быть больше нуля")

    wallet = await session.get(Wallet, wallet_id)
    if wallet is None or wallet.status != "active":
        raise CounterpartyPaymentError("Счёт не найден или неактивен")

    resolved_article_id = article_id
    if resolved_article_id is None:
        resolved_article_id = await session.scalar(
            select(DdsArticle.id).where(DdsArticle.code == PREPAYMENT_ARTICLE_CODE)
        )
    elif await session.get(DdsArticle, resolved_article_id) is None:
        raise CounterpartyPaymentError("Статья ДДС не найдена")

    transaction = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=amt,
        operation_date=operation_date,
        article_id=resolved_article_id,
        counterparty_id=counterparty_id,
        source_kind="supplier_prepayment",
        payment_purpose=f"Предоплата поставщику {cp.name}",
        comment=note,
        quality_status="final",
    )
    session.add(transaction)
    await session.flush()

    prepayment = SupplierPrepayment(
        counterparty_id=counterparty_id,
        kind=kind,
        wallet_id=wallet.id,
        amount=amt,
        amount_settled=Decimal("0.00"),
        status="open",
        cashflow_transaction_id=transaction.id,
        article_id=resolved_article_id,
        note=note,
        created_by_user_id=actor_user_id,
    )
    session.add(prepayment)
    await session.flush()
    transaction.source_id = prepayment.id  # обратная ссылка денежный факт → предоплата
    await session.commit()
    await session.refresh(prepayment)
    return prepayment


async def create_opening_prepayment(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    amount: Decimal,
    kind: str = "other",
    note: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> SupplierPrepayment:
    """Начальный остаток «денег у поставщика» (рекламный кабинет, депозит ЛК и т.п.).

    Деньги ушли ИСТОРИЧЕСКИ, до внедрения системы — кошелёк не трогаем и
    CashflowTransaction НЕ создаём (иначе задвоили бы расход в ДДС). Дальше остаток
    живёт как обычная дебиторка: закрывающие УПД гасят его автоматически, реестр
    показывает в «Предоплатах». Суммы ведём с НДС (gross) — как оплаты и счета."""
    cp = await session.get(Counterparty, counterparty_id)
    if cp is None:
        raise CounterpartyPaymentError("Контрагент не найден")
    amt = _money(amount)
    if amt <= 0:
        raise CounterpartyPaymentError("Сумма начального остатка должна быть больше нуля")

    prepayment = SupplierPrepayment(
        counterparty_id=counterparty_id,
        kind=kind,
        wallet_id=None,
        amount=amt,
        amount_settled=Decimal("0.00"),
        status="open",
        cashflow_transaction_id=None,
        # Единственное место, где дебиторка объявляется САМОСТОЯТЕЛЬНЫМ денежным фактом:
        # деньги ушли до внедрения системы, проводки под них нет и не будет. Все остальные
        # предоплаты выводятся из уже учтённого факта, и в сверке вторыми строками не идут.
        opening=True,
        note=note or "Начальный остаток (до внедрения системы)",
        created_by_user_id=actor_user_id,
    )
    session.add(prepayment)
    await session.flush()
    await session.commit()
    await session.refresh(prepayment)
    return prepayment


def _sync_bill_prepayment_status(prepayment: SupplierPrepayment) -> None:
    """Статус ДЗ по счёту из amount/amount_settled (refunded не трогаем)."""
    if prepayment.status == "refunded":
        return
    settled = _money(prepayment.amount_settled)
    if settled >= _money(prepayment.amount):
        prepayment.status = "settled"
    elif settled > 0:
        prepayment.status = "partially_settled"
    else:
        prepayment.status = "open"


async def _unwind_bill_prepayment_settlements(
    session: AsyncSession,
    prepayment: SupplierPrepayment,
    *,
    target_settled: Decimal,
) -> None:
    """Откатить зачёты закрывающих, профинансированные этой prepaid_bill ДЗ, пока её amount_settled
    не опустится до target_settled — оплату счёта уменьшили НИЖЕ уже зачтённого закрывающими (напр.
    пере-разбор той же банк-операции убрал строку по счёту). Погашенные закрывающие возвращаются в
    кредиторку (``_recompute_status``), иначе долг поставщику занижен фантомно-погашенным УПД. LIFO
    по дате зачёта. Замороженный в банк-черновике закрывающий не трогаем (редкий край) — иначе
    расфризили бы чужую отправку."""
    tgt = _money(target_settled)
    if _money(prepayment.amount_settled) <= tgt:
        return
    allocs = (
        await session.scalars(
            select(InvoicePaymentAllocation)
            .where(
                InvoicePaymentAllocation.prepayment_id == prepayment.id,
                InvoicePaymentAllocation.source_kind == "prepayment",
            )
            .order_by(InvoicePaymentAllocation.created_at.desc())
        )
    ).all()
    for alloc in allocs:
        if _money(prepayment.amount_settled) <= tgt:
            break
        closing = await session.get(SupplierInvoice, alloc.invoice_id)
        if closing is not None and closing.draft_id is not None:
            continue  # заморожен в банк-черновике — пропускаем
        excess = _money(prepayment.amount_settled) - tgt
        take = min(_money(alloc.amount), excess)
        if take >= _money(alloc.amount):
            await session.delete(alloc)
        else:
            alloc.amount = _money(alloc.amount) - take
        prepayment.amount_settled = _money(prepayment.amount_settled) - take
        await session.flush()
        if closing is not None:
            await _recompute_status(session, closing)


async def release_invoice_prepayment_allocations(
    session: AsyncSession, invoice: SupplierInvoice
) -> Decimal:
    """Вернуть предоплатам зачёты, сделанные ЭТОЙ накладной (обратная операция к
    ``_allocate_invoice_from_prepayment``).

    Для случаев, когда документ перестаёт быть основанием зачёта: аннулирование/удаление
    почтового закрывающего, уход его даты в будущее (правило 4). Аллокации
    ``source_kind='prepayment'`` удаляются, ``amount_settled`` финансировавших предоплат
    уменьшается, их статус пересинхронизируется — иначе аванс остаётся «съеденным»
    несуществующим документом и ДЗ занижена. Денег не двигает; статус самой накладной не
    трогает — вызывающий решает сам (void/удаление/пересчёт). Возвращает сумму возврата."""
    allocations = (
        await session.scalars(
            select(InvoicePaymentAllocation).where(
                InvoicePaymentAllocation.invoice_id == invoice.id,
                InvoicePaymentAllocation.source_kind == "prepayment",
            )
        )
    ).all()
    released = Decimal("0.00")
    for alloc in allocations:
        prepayment = (
            await session.get(SupplierPrepayment, alloc.prepayment_id)
            if alloc.prepayment_id is not None
            else None
        )
        await session.delete(alloc)
        if prepayment is not None:
            prepayment.amount_settled = max(
                _money(prepayment.amount_settled) - _money(alloc.amount), Decimal("0.00")
            )
            _sync_bill_prepayment_status(prepayment)
        released += _money(alloc.amount)
    if released > 0:
        await session.flush()
    return released


async def release_closing_prepayment_excess(
    session: AsyncSession, invoice: SupplierInvoice
) -> Decimal:
    """Вернуть предоплатам ИЗБЫТОК зачёта после уменьшения суммы закрывающего.

    Пере-разбор письма может исправить сумму документа ВНИЗ (опечатка распознавания). Уже
    сделанные авансовые зачёты тогда превышают документ — аванс «съеден» сверх основания, ДЗ
    занижена молча. Возвращаем избыток ТОЛЬКО из prepayment-аллокаций (LIFO): реальные деньги
    (cash/bank) правка суммы не трогает — их разбирают контуры оплат. Обратная операция —
    дозачёт при росте суммы — уже есть в ``apply_closing_document`` (auto_settle остатка)."""
    total = await session.scalar(
        select(func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0)).where(
            InvoicePaymentAllocation.invoice_id == invoice.id
        )
    )
    excess = _money(total) - _money(invoice.amount)
    if excess <= 0:
        return Decimal("0.00")
    allocations = (
        await session.scalars(
            select(InvoicePaymentAllocation)
            .where(
                InvoicePaymentAllocation.invoice_id == invoice.id,
                InvoicePaymentAllocation.source_kind == "prepayment",
            )
            .order_by(InvoicePaymentAllocation.created_at.desc())
        )
    ).all()
    released = Decimal("0.00")
    for alloc in allocations:
        if excess <= 0:
            break
        take = min(_money(alloc.amount), excess)
        prepayment = (
            await session.get(SupplierPrepayment, alloc.prepayment_id)
            if alloc.prepayment_id is not None
            else None
        )
        if take >= _money(alloc.amount):
            await session.delete(alloc)
        else:
            alloc.amount = _money(alloc.amount) - take
        if prepayment is not None:
            prepayment.amount_settled = max(
                _money(prepayment.amount_settled) - take, Decimal("0.00")
            )
            _sync_bill_prepayment_status(prepayment)
        excess -= take
        released += take
    if released > 0:
        await session.flush()
    return released


async def _bill_paid_already_receivable(session: AsyncSession, invoice: SupplierInvoice) -> Decimal:
    """Часть оплаты счёта, деньги которой УЖЕ числятся дебиторкой от правила 1.

    Штатный порядок банк-фида — classify-then-match: сначала классификатор безусловно проводит
    платёж через правило 1 (нет открытой кредиторки → вся сумма становится авансом, ключ
    ``cashflow_transaction_id``), и только потом оператор привязывает ТУ ЖЕ операцию к счёту.
    Без этой поправки чокпоинт заводил бы вторую дебиторку на те же деньги (ключ
    ``bill_invoice_id``), и плитка показывала бы 2× платежа, а пришедший УПД гасил бы лишь одну
    из двух — вторая висела бы фантомом вечно.

    Считаем по КАЖДОЙ оплатной аллокации счёта, чей платёж уже несёт rule-1-предоплату, и не
    вычитаем по одной проводке больше суммы этой предоплаты (бюджет на проводку) — иначе две
    аллокации одного платежа списали бы покрытие дважды.
    """
    allocations = (
        await session.scalars(
            select(InvoicePaymentAllocation)
            .where(
                InvoicePaymentAllocation.invoice_id == invoice.id,
                InvoicePaymentAllocation.source_kind.in_(("cash", "bank")),
            )
            .order_by(InvoicePaymentAllocation.created_at)
        )
    ).all()
    budget_by_tx: dict[uuid.UUID, Decimal] = {}
    covered = Decimal("0.00")
    for alloc in allocations:
        transaction_id = alloc.cashflow_transaction_id
        if transaction_id is None and alloc.bank_operation_id is not None:
            operation = await session.get(BankOperation, alloc.bank_operation_id)
            transaction_id = operation.cashflow_transaction_id if operation is not None else None
        if transaction_id is None:
            continue
        if transaction_id not in budget_by_tx:
            rule1 = await session.scalar(
                select(SupplierPrepayment)
                .where(
                    SupplierPrepayment.cashflow_transaction_id == transaction_id,
                    SupplierPrepayment.kind != BILL_PREPAYMENT_KIND,
                )
                .limit(1)
            )
            # Берём ПОЛНУЮ сумму rule-1-предоплаты, а не её открытый остаток, и НЕ фильтруем по
            # статусу: вопрос «становились ли эти деньги дебиторкой», а не «числятся ли ею
            # сейчас». Иначе пришедший УПД гасит rule-1-ДЗ (статус 'settled'), покрытие пропадает,
            # и следующее же касание счёта завело бы фантомную ДЗ на те же деньги. Если предоплату
            # СНЕСЛИ (реклассификация/исключение), покрытия действительно нет — счёт корректно
            # берёт дебиторку на себя.
            budget_by_tx[transaction_id] = (
                _money(rule1.amount) if rule1 is not None else Decimal("0.00")
            )
        take = min(_money(alloc.amount), budget_by_tx[transaction_id])
        if take > 0:
            covered += take
            budget_by_tx[transaction_id] -= take
    return covered


async def reconcile_bill_prepayment(
    session: AsyncSession,
    invoice: SupplierInvoice,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    """ЕДИНЫЙ ЧОКПОИНТ канона ДЗ/КЗ (владелец 17.07): держать дебиторку по оплаченному счёту в
    синхроне с его оплаченной суммой. Вызывается из ``_recompute_status`` для КАЖДОГО счёта
    (doc_kind='bill') — поэтому любая дверь гашения (черновик, ручная оплата, банковская сверка,
    авто-match, сплит, via-Сейф, …) автоматически заводит ДЗ, без индивидуальных правок.

    Счёт — не долг: его оплата — предоплата поставщику (деньги ушли, закрывающего документа ещё
    нет). Заводим/синхронизируем ДЗ ``kind='prepaid_bill'``, привязанную к счёту
    (``bill_invoice_id`` → идемпотентность: одна ДЗ на счёт). Денег НЕ двигает — факт уже несёт
    аллокация счёта на реальную проводку, поэтому ДЗ «начального-остатка» вида
    (``cashflow_transaction_id=None``); иначе задвоили бы расход. Симметрично: рост оплаты растит
    ДЗ и неттит уже открытую кредиторку (правило 2 в ОБРАТНОМ порядке — закрывающий УПД пришёл
    раньше оплаты счёта); УМЕНЬШЕНИЕ оплаты ниже уже зачтённого откатывает зачёт закрывающего
    (возвращает его в КЗ), иначе долг поставщику занижен. Прямой порядок (оплата → потом УПД)
    закрывается со стороны закрывающего (``apply_closing_document``). Идемпотентно, без commit."""
    if invoice.doc_kind != "bill":
        return
    paid = _money(invoice.amount) - await _invoice_remaining(session, invoice)
    existing = await session.scalar(
        select(SupplierPrepayment)
        .where(
            SupplierPrepayment.bill_invoice_id == invoice.id,
            SupplierPrepayment.kind == BILL_PREPAYMENT_KIND,
        )
        .limit(1)
    )
    # Носитель ДЗ у денег платежа ровно ОДИН. Если своя prepaid_bill-запись у счёта уже есть —
    # ею и управляем (правило 1 такие оплаты в свою предоплату не включает: см. carried в
    # ensure_prepayment_from_bank_transaction). Уступаем правилу 1 только при решении о
    # СОЗДАНИИ: платёж, уже несущий rule-1-предоплату, свою ДЗ имеет — вторую не заводим.
    # ``paid`` остаётся СЫРЫМ для отката зачётов — откат обязан следовать реальной оплате
    # счёта, иначе снимались бы зачёты закрывающих, которые никто не заменит.
    booked = paid
    if existing is None:
        booked = max(paid - await _bill_paid_already_receivable(session, invoice), Decimal("0.00"))

    # Оплату счёта уменьшили ниже уже зачтённого закрывающими → откатить избыток зачётов
    # (закрывающие обратно в КЗ), иначе они остались бы фантомно-погашенными, долг поставщику
    # занижен. После отката amount_settled <= paid, и CHECK amount>=amount_settled не нарушится.
    if existing is not None and _money(existing.amount_settled) > max(paid, Decimal("0.00")):
        await _unwind_bill_prepayment_settlements(
            session, existing, target_settled=max(paid, Decimal("0.00"))
        )

    if paid <= 0:
        # Оплату счёта откатили полностью: после отката зачётов нетронутую ДЗ удаляем
        # (тронутую — если остался замороженный в черновике зачёт — оставляем).
        if existing is not None:
            _sync_bill_prepayment_status(existing)
            if _prepayment_untouched(existing):
                await session.delete(existing)
                await session.flush()
        return

    if booked <= 0:
        # Достижимо только при existing is None (иначе booked=paid>0): счёт оплачен, но все его
        # деньги уже несёт rule-1-предоплата — свою ДЗ счёт не заводит (иначе задвоение).
        return

    grew = False
    if existing is None:
        number = (invoice.number or "").strip()
        candidate = SupplierPrepayment(
            counterparty_id=invoice.counterparty_id,
            kind=BILL_PREPAYMENT_KIND,
            wallet_id=None,
            amount=booked,
            amount_settled=Decimal("0.00"),
            status="open",
            cashflow_transaction_id=None,
            bill_invoice_id=invoice.id,
            article_id=invoice.dds_article_id,
            # Период услуги СЧЁТ уже знает: он распознан из его текста («за июль» → 01.07–31.07)
            # либо проставлен руками при разборе. Дебиторка ссылалась на этот счёт полем
            # bill_invoice_id и period с него не читала — период лежал рядом, а очередь
            # признания показывала «Период не указан» и ждала, пока человек введёт его заново.
            # Хуже того: у контрагента, который УПД не присылает («счёт за период»), ждать было
            # нечего вовсе — расход не признавался никогда, пока строку не трогали руками.
            **_invoice_period_fields(invoice),
            note=f"Предоплата по счёту {number}" if number else "Предоплата по счёту",
            created_by_user_id=actor_user_id,
        )
        # Вставка в savepoint: гонка двух дверей на один счёт отклоняется частичным UNIQUE
        # (uq_supplier_prepayment_bill). Ловим — берём уже заведённую ДЗ и синхронизируем её как
        # existing, не роняя транзакцию двери (её работа — аллокации/проводки — сохраняется).
        try:
            async with session.begin_nested():
                session.add(candidate)
                await session.flush()
            existing = candidate
            grew = True
        except IntegrityError:
            # begin_nested откатил savepoint (внешняя транзакция двери жива, её аллокации уже
            # зафлашены до savepoint — откат хирургичен). expunge снимает candidate из session.new,
            # иначе он пере-вставится на финальном commit. Перечит видит выигравшую строку при
            # READ COMMITTED (дефолт PG); при более строгой изоляции снапшот мог бы её не увидеть.
            session.expunge(candidate)
            existing = await session.scalar(
                select(SupplierPrepayment)
                .where(
                    SupplierPrepayment.bill_invoice_id == invoice.id,
                    SupplierPrepayment.kind == BILL_PREPAYMENT_KIND,
                )
                .limit(1)
            )
            if existing is None:
                raise

    if not grew and existing is not None:
        # Существующая (или подхваченная после гонки) ДЗ: синхронизируем к оплаченной сумме.
        # amount не опускаем ниже уже зачтённого — иначе нарушим CHECK amount>=amount_settled
        # (замороженный в черновике зачёт _unwind не снял, settled мог остаться > paid).
        if booked > _money(existing.amount):
            grew = True
        existing.amount = max(booked, _money(existing.amount_settled))
        _sync_bill_prepayment_status(existing)
        await session.flush()

    # Обратный порядок: новая/выросшая открытая ДЗ гасит уже открытую кредиторку контрагента.
    if grew and existing.status in OPEN_PREPAYMENT_STATUSES:
        await _settle_counterparty_closing_from_prepayments(
            session, invoice.counterparty_id, actor_user_id=actor_user_id
        )


async def _settle_counterparty_closing_from_prepayments(
    session: AsyncSession,
    counterparty_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    """Правило 2 в обратном порядке для финансовых документов.

    Открытые финансовые УПД/акты гасятся предоплатами FIFO, когда предоплата появилась позже
    кредиторки. Складские накладные намеренно исключены: их можно зачесть только явным вызовом
    ``settle_invoice_from_prepayment`` после выбора оператором конкретной поставки.
    """
    closings = (
        await session.scalars(
            select(SupplierInvoice)
            .where(
                SupplierInvoice.counterparty_id == counterparty_id,
                SupplierInvoice.direction == "payable",
                SupplierInvoice.doc_kind == "closing",
                SupplierInvoice.operational_scope == AUTO_SETTLEMENT_OPERATIONAL_SCOPE,
                invoice_binds_settlement(),
                SupplierInvoice.barter_role.is_(None),
                SupplierInvoice.payment_status.in_(UNPAID_INVOICE_STATUSES),
            )
            .order_by(SupplierInvoice.invoice_date.nulls_last(), SupplierInvoice.created_at)
        )
    ).all()
    for closing in closings:
        await auto_settle_invoice_from_open_prepayments(
            session, closing, actor_user_id=actor_user_id
        )


async def _settle_open_kz_from_transaction(
    session: AsyncSession,
    transaction: CashflowTransaction,
    *,
    limit: Decimal,
    actor_user_id: uuid.UUID | None = None,
) -> Decimal:
    """Правило 1: деньги транзакции FIFO-гасят открытую финансовую кредиторку контрагента.

    Открытая КЗ здесь = неоплаченный остаток АКТИВНЫХ финансовых УПД/актов. Складские накладные
    не подбираются автоматически даже при совпадении поставщика: платёж без ссылки на конкретную
    товарную поставку становится ДЗ, а ручной зачёт выполняется отдельно. Аллокация
    source_kind='cash' денег НЕ двигает (они ушли транзакцией). Возвращает погашенную сумму
    (≤ limit).

    Документ, уже отправленный в банк-черновик (``draft_id``), НЕ подбираем: по нему платёж
    в пути, и гашение его чужими деньгами привело бы к оплате дважды — сначала этим платежом,
    потом исполнением черновика. Тот же гард стоит в ``auto_settle_invoice_from_open_prepayments``;
    здесь его не было, и любой свободный платёж тому же поставщику мог закрыть документ,
    ожидающий подтверждения в банке."""
    pool = _money(limit)
    settled = Decimal("0.00")
    if pool <= 0:
        return settled
    invoices = (
        await session.scalars(
            select(SupplierInvoice)
            .where(
                SupplierInvoice.counterparty_id == transaction.counterparty_id,
                SupplierInvoice.direction == "payable",
                SupplierInvoice.doc_kind == "closing",
                SupplierInvoice.operational_scope == AUTO_SETTLEMENT_OPERATIONAL_SCOPE,
                invoice_binds_settlement(),
                SupplierInvoice.barter_role.is_(None),
                SupplierInvoice.draft_id.is_(None),
                SupplierInvoice.payment_status.in_(UNPAID_INVOICE_STATUSES),
            )
            .order_by(SupplierInvoice.invoice_date.nulls_last(), SupplierInvoice.created_at)
        )
    ).all()
    for invoice in invoices:
        if pool <= 0:
            break
        remaining = await _invoice_remaining(session, invoice)
        if remaining <= 0:
            continue
        alloc = min(remaining, pool)
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id,
                source_kind="cash",
                cashflow_transaction_id=transaction.id,
                amount=alloc,
                created_by_user_id=actor_user_id,
            )
        )
        await session.flush()
        await _recompute_status(session, invoice)
        pool -= alloc
        settled += alloc
    return settled


async def _unwind_transaction_kz_settlements(
    session: AsyncSession, transaction_id: uuid.UUID, *, include_bills: bool
) -> bool:
    """Снять cash-аллокации этой транзакции. Два режима — по тому, ЧТО происходит с деньгами.

    ``include_bills=False`` — ПЕРЕСБОРКА (правило 1 пере-раскладывает своё): снимаем только
    зачёты ЗАКРЫВАЮЩИХ (их правило 1 и создаёт). Ручная оплата СЧЁТА оператором
    (``allocate_cash_to_invoice`` может нести тот же ``cashflow_transaction_id``) — не наша
    запись: снять её значило бы вернуть оплаченный счёт в очередь оплат (риск повторной
    оплаты). Прежний код снимал ВСЁ подряд, исходя из ложного допущения «вручную к накладным
    банк-фид проводки не аллоцируют».

    ``include_bills=True`` — СНОС (исключение операции / переразбор сплитом / отмена
    предоплаты — деньги уходят из учёта): снимаем все cash-аллокации, включая счета; их
    ``_recompute_status`` → чокпоинт сам приберёт осиротевшую ДЗ по счёту.

    Возвращает False, если затронутая накладная «заморожена» (ушла в банк-черновик) — тогда
    историю не трогаем, вызывающий блокирует операцию понятной ошибкой."""
    allocs = list(
        (
            await session.scalars(
                select(InvoicePaymentAllocation).where(
                    InvoicePaymentAllocation.cashflow_transaction_id == transaction_id,
                    InvoicePaymentAllocation.source_kind == "cash",
                )
            )
        ).all()
    )
    if not allocs:
        return True
    invoice_ids = {a.invoice_id for a in allocs}
    invoices = list(
        (
            await session.scalars(
                select(SupplierInvoice).where(SupplierInvoice.id.in_(invoice_ids))
            )
        ).all()
    )
    if not include_bills:
        # Пересборка правила 1 владеет только своими финансовыми FIFO-зачётами. Складскую
        # cash-аллокацию мог явно сделать оператор; отличительного флага у старых записей нет,
        # поэтому трогать её при повторной классификации банковской проводки нельзя.
        closing_ids = {
            inv.id
            for inv in invoices
            if inv.doc_kind == "closing"
            and inv.operational_scope == AUTO_SETTLEMENT_OPERATIONAL_SCOPE
        }
        allocs = [a for a in allocs if a.invoice_id in closing_ids]
        invoices = [inv for inv in invoices if inv.id in closing_ids]
        if not allocs:
            return True
    if any(inv.draft_id is not None for inv in invoices):
        return False
    for alloc in allocs:
        await session.delete(alloc)
    await session.flush()
    for inv in invoices:
        await _recompute_status(session, inv)
    return True


async def unwind_operation_bank_allocations(
    session: AsyncSession, bank_operation_id: uuid.UUID
) -> bool:
    """Снять bank-аллокации операции (сверка «операция ↔ накладная») при её исключении из учёта.

    Симметрия сноса: cash-зачёты проводки снимает ``_unwind_transaction_kz_settlements``, но
    сверка метит оплату накладных ДРУГИМ ключом — ``bank_operation_id``. Без этого отката
    «Исключить операцию»/«Внутренний перевод» оставляли накладную оплаченной деньгами, которых
    в учёте больше нет, а ДЗ оплаченного счёта висела без единого рубля за ней. Пересчёт
    статуса накладной дальше сам прибирает ДЗ (чокпоинт). False — если накладная заморожена
    в банк-черновике: снять нельзя, вызывающий блокирует операцию понятной ошибкой."""
    allocs = list(
        (
            await session.scalars(
                select(InvoicePaymentAllocation).where(
                    InvoicePaymentAllocation.bank_operation_id == bank_operation_id,
                    InvoicePaymentAllocation.source_kind == "bank",
                )
            )
        ).all()
    )
    if not allocs:
        return True
    invoice_ids = {a.invoice_id for a in allocs}
    invoices = list(
        (
            await session.scalars(
                select(SupplierInvoice).where(SupplierInvoice.id.in_(invoice_ids))
            )
        ).all()
    )
    if any(inv.draft_id is not None for inv in invoices):
        return False
    for alloc in allocs:
        await session.delete(alloc)
    await session.flush()
    for inv in invoices:
        await _recompute_status(session, inv)
    return True


async def _transaction_carried_bill_allocations(
    session: AsyncSession, transaction: CashflowTransaction
) -> Decimal:
    """Оплаты СЧЕТОВ из этого платежа, дебиторку которых несёт правило 1 (а не чокпоинт).

    Оплата счёта по канону — предоплата. Носитель ДЗ ровно один: если у счёта ЕСТЬ своя
    prepaid_bill-запись (оплата легла раньше классификации — чокпоинт успел первым), правило 1
    эти деньги в свою предоплату не включает; если записи НЕТ (классификация была первой —
    чокпоинт увидел rule-1-предоплату и уступил), их несёт правило 1. Считаем вторую группу:
    аллокации счетов обоими ключами платежа (cash-проводкой и bank-операцией через мост),
    у чьих счетов prepaid_bill-записи нет.

    Мост «проводка → операция» тянет только НЕатрибутированные аллокации: у разбора операции по
    разным контрагентам гашения долей помечены своей проводкой, и чужая доля не должна попасть
    в бюджет этой (иначе её дебиторка занижена — см. ``payment_allocated_amount``)."""
    operation_ids = select(BankOperation.id).where(
        BankOperation.cashflow_transaction_id == transaction.id
    )
    own_prepaid = (
        select(SupplierPrepayment.id)
        .where(
            SupplierPrepayment.bill_invoice_id == InvoicePaymentAllocation.invoice_id,
            SupplierPrepayment.kind == BILL_PREPAYMENT_KIND,
        )
        .exists()
    )
    total = await session.scalar(
        select(func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0))
        .select_from(InvoicePaymentAllocation)
        .join(SupplierInvoice, SupplierInvoice.id == InvoicePaymentAllocation.invoice_id)
        .where(
            SupplierInvoice.doc_kind == "bill",
            InvoicePaymentAllocation.source_kind != "prepayment",
            (InvoicePaymentAllocation.cashflow_transaction_id == transaction.id)
            | (
                InvoicePaymentAllocation.bank_operation_id.in_(operation_ids)
                & InvoicePaymentAllocation.cashflow_transaction_id.is_(None)
            ),
            ~own_prepaid,
        )
    )
    return _money(total)


def _invoice_period_fields(invoice: SupplierInvoice) -> dict[str, object]:
    """Поля периода услуги, унаследованные дебиторкой от оплаченного счёта.

    Счёт — первичный источник периода для абонентских услуг: «Услуги связи за июль»
    распознаётся при приёме из почты и лежит в ``service_period_start/end`` со статусом
    ``ready``. Дебиторке этот период нужен ровно для того, ради чего он и распознавался, —
    чтобы по окончании месяца расход признался сам.

    ``service_period_months`` не наследуем: у счёта его нет, а вычислять из дат бессмысленно —
    помесячное разложение включается либо галкой в «Новом платеже», либо режимом карточки
    «счёт за период», и оба пути считают месяцы сами.
    """
    if (
        invoice.service_period_status != "ready"
        or invoice.service_period_start is None
        or invoice.service_period_end is None
    ):
        return {}
    return {
        "service_period_start": invoice.service_period_start,
        "service_period_end": invoice.service_period_end,
        "service_period_status": "ready",
    }


async def _one_off_counterparty(session: AsyncSession, counterparty_id: uuid.UUID | None) -> bool:
    """Режим карточки «разовые работы»: закрывающих не ждём и расход признаём сразу."""
    if counterparty_id is None:
        return False
    mode = await session.scalar(
        select(CounterpartyPayableProfile.service_billing_mode).where(
            CounterpartyPayableProfile.counterparty_id == counterparty_id
        )
    )
    return mode == "one_off"


async def _paid_bill_period(
    session: AsyncSession, transaction: CashflowTransaction, amount: Decimal
) -> tuple[date, date] | None:
    """Период услуги со СЧЁТА, который этот платёж оплатил.

    Второй мост после ``_expense_line_period``, и нужен он потому, что первый работает только
    для платежей из черновика (``source_kind='counterparty_payment'``). Реальные платежи
    приходят иначе — банковской выпиской: черновик ушёл в банк, банк исполнил, выписка
    вернулась, и дебиторка родилась из проводки, ничего не знающей о счёте. На проде так
    возникли ВСЕ строки очереди «Ждём документ»: АЙКО, ДоксИнБокс, Манго, Лемма.

    Ищем строго: тот же контрагент, точная сумма, счёт (``doc_kind='bill'``) с ГОТОВЫМ
    периодом. Точное совпадение суммы — сильный признак: абонентские счета выставляются на
    фиксированную сумму, и промахнуться ею мимо своего счёта трудно. Если подходящих счетов
    несколько с РАЗНЫМИ периодами — не берём ни один: угадать хуже, чем спросить человека.
    """
    if transaction.counterparty_id is None:
        return None
    rows = list(
        (
            await session.scalars(
                select(SupplierInvoice).where(
                    SupplierInvoice.counterparty_id == transaction.counterparty_id,
                    SupplierInvoice.doc_kind == "bill",
                    SupplierInvoice.direction == "payable",
                    SupplierInvoice.payment_status != "void",
                    SupplierInvoice.service_period_status == "ready",
                    SupplierInvoice.service_period_start.is_not(None),
                    SupplierInvoice.service_period_end.is_not(None),
                    SupplierInvoice.amount == _money(amount),
                )
            )
        ).all()
    )
    periods = {(row.service_period_start, row.service_period_end) for row in rows}
    if len(periods) != 1:
        return None
    return periods.pop()


async def _expense_line_period(
    session: AsyncSession, transaction: CashflowTransaction
) -> tuple[date, date, int | None, bool] | None:
    """Период услуги со строки расхода, породившей платёж, если он там указан.

    Окно «Новый платёж» спрашивает период на строке и гарантирует, что у всех строк одного
    черновика он общий (гард «платежи с разными периодами оформите отдельными черновиками»),
    поэтому берём первую строку с заполненным периодом. Возвращает ещё число месяцев и
    признак помесячного признания: без них дебиторка за квартал не знала бы, что её надо
    гасить по третям.
    """
    if transaction.source_kind != "counterparty_payment" or transaction.source_id is None:
        return None
    line = await session.scalar(
        select(ExpenseDraftLine)
        .where(
            ExpenseDraftLine.draft_id == transaction.source_id,
            ExpenseDraftLine.service_period_start.is_not(None),
            ExpenseDraftLine.service_period_end.is_not(None),
        )
        .order_by(ExpenseDraftLine.position)
    )
    if line is None or line.service_period_start is None or line.service_period_end is None:
        return None
    return (
        line.service_period_start,
        line.service_period_end,
        line.service_period_months,
        bool(line.auto_recognize_monthly),
    )


async def ensure_prepayment_from_bank_transaction(
    session: AsyncSession, transaction: CashflowTransaction
) -> SupplierPrepayment | None:
    """Синхронизировать распределение платежа поставщику с состоянием проводки (правило 1 канона).

    Свободный платёж поставщику (не через счёт-черновик — те короткозамыкаются на pre-booked
    ДО этого хука) сначала ГАСИТ открытую кредиторку только по финансовым УПД/актам, а ИЗЛИШЕК
    становится дебиторкой (предоплатой). Товарные накладные платежом без явной ссылки не
    закрываются: весь свободный остаток становится ДЗ до ручного зачёта конкретной поставки.
    Ограничение — только поставщики (есть payable-профиль); у сотрудников/налоговой/банков его нет,
    их платежи сюда не попадают. Флаг ``bank_payments_create_prepayment`` контур больше НЕ гейтит.
    Новую ДДС-проводку НЕ создаём — деньги уже учтены самой проводкой; движем только зачёты КЗ
    (cash-аллокации, денег не двигают) и запись предоплаты.

    Канон владельца не различает КАНАЛ денег: наличная выплата из Сейфа — такой же платёж, как
    банковское списание, и точно так же обязана давать дебиторку (имя функции историческое —
    контур начинался с банк-фида). Исключены только самооплатные контуры (``kassa_cheque``):
    чек — покупка с товаром на руках, он сам заводит и сам закрывает свою накладную, дебиторка
    там означала бы несуществующий долг поставщика перед нами.

    Не «создать один раз», а ПРИВЕСТИ распределение к текущей проводке — переклассификация
    не должна оставлять фантом:
      • транзакция больше не квалифицируется (сняли контрагента / стала приходом / исключена из
        ДДС / контрагент без payable-профиля) → снимаем нетронутую предоплату И её зачёты КЗ;
      • контрагент/сумма/кошелёк изменились → пересобираем зачёты + предоплату;
      • гашёную предоплату (amount_settled>0) или замороженные зачёты (накладная в черновике)
        не трогаем — историю сохраняем.
    Идемпотентно: одна транзакция = один набор зачётов КЗ + одна предоплата на остаток."""
    if transaction.source_kind in SELF_SETTLING_SOURCE_KINDS:
        # Ранний выход БЕЗ побочек: у чека свои аллокации, и снимать их правило 1 не вправе.
        return None
    existing = await session.scalar(
        select(SupplierPrepayment).where(
            SupplierPrepayment.cashflow_transaction_id == transaction.id
        )
    )
    if existing is not None and existing.kind in FOREIGN_PREPAYMENT_KINDS:
        # Предоплату завёл ДРУГОЙ контур и распорядился деньгами по-своему: целевой аванс ждёт
        # свою поставку, ДЗ оплаченного счёта принадлежит чокпоинту. Поиск по
        # cashflow_transaction_id находит их наравне со своими, но пересобирать чужую запись
        # (а тем более удалить её и скормить деньги FIFO постороннему УПД) правило 1 не вправе.
        return existing
    should_have = (
        transaction.direction == "out"
        and transaction.counterparty_id is not None
        # Исключённая из ДДС проводка (мягкое «Исключить») из баланса кошелька уже убрана —
        # значит и денег для дебиторки в ней нет: иначе ДЗ пережила бы отмену платежа.
        and transaction.quality_status != EXCLUDED_QUALITY
    )
    if should_have:
        # Только поставщик (payable-профиль). Флаг больше не гейтит — канон универсален.
        profile_exists = await session.scalar(
            select(CounterpartyPayableProfile.counterparty_id).where(
                CounterpartyPayableProfile.counterparty_id == transaction.counterparty_id
            )
        )
        should_have = profile_exists is not None

    if not should_have:
        # Транзакция больше не квалифицируется (сняли контрагента / сменили на не-предоплатного /
        # стала приходом) — деньги уходят из учёта, снимаем ВСЁ, что они финансировали: и зачёты
        # КЗ правила 1, и оплату счетов (их чокпоинт приберёт свою ДЗ при пересчёте). Замороженный
        # в банк-черновике зачёт снять нельзя — блокируем понятной ошибкой.
        if not await _unwind_transaction_kz_settlements(
            session, transaction.id, include_bills=True
        ):
            raise CounterpartyPaymentError(
                "Платёж уже погасил кредиторку, отправленную в банк-черновик — "
                "сначала откатите черновик"
            )
        if existing is not None and _prepayment_untouched(existing):
            await session.delete(existing)
            await session.flush()
        return None

    # Гашёную предоплату (её авансы уже связаны с накладными) не пересобираем.
    if existing is not None and not _prepayment_untouched(existing):
        return existing

    # ПЕРЕСБОРКА своего: снимаем только зачёты закрывающих (bills не трогаем — ручную оплату
    # счёта оператором пересборка правила 1 сносить не вправе). Дальше распределение считается
    # от «бюджета платежа»: сколько денег проводки ещё НЕ занято выжившими аллокациями
    # (оплаты счетов + bank-аллокации закрывающих через мост операции).
    if not await _unwind_transaction_kz_settlements(session, transaction.id, include_bills=False):
        # Замороженный зачёт (накладная в банк-черновике): историю не трогаем, синхронизируем
        # только поля нетронутой предоплаты той же арифметикой бюджета (занятое — вычитается,
        # счета без своей prepaid_bill-записи — остаются на правиле 1).
        if existing is not None:
            consumed = await payment_allocated_amount(session, transaction_id=transaction.id)
            carried = await _transaction_carried_bill_allocations(session, transaction)
            remainder = _money(transaction.amount) - consumed + carried
            if remainder <= 0:
                await session.delete(existing)
                await session.flush()
                return None
            existing.counterparty_id = transaction.counterparty_id
            existing.amount = remainder
            existing.article_id = transaction.article_id
            existing.wallet_id = transaction.wallet_id
            await session.flush()
        return existing

    amount = _money(transaction.amount)
    # Свободный бюджет платежа на гашение КЗ: сумма минус уже занятые деньги (оплаты счетов;
    # bank-аллокации закрывающих, если операцию сверили напрямую). Иначе один платёж гасил бы
    # документов больше, чем он есть (перерасход).
    consumed = await payment_allocated_amount(session, transaction_id=transaction.id)
    settled = await _settle_open_kz_from_transaction(session, transaction, limit=amount - consumed)
    # Дебиторка правила 1 = незанятый остаток ПЛЮС оплаты счетов, чью ДЗ несёт правило 1
    # (счета без своей prepaid_bill-записи): оплата счёта — тоже предоплата по канону.
    carried = await _transaction_carried_bill_allocations(session, transaction)
    remainder = amount - consumed - settled + carried
    if remainder <= 0:
        # Платёж целиком ушёл на гашение кредиторки — дебиторки (предоплаты) не возникает;
        # нетронутую предоплату этой транзакции снимаем (фантом на разницу).
        if existing is not None:
            await session.delete(existing)
            await session.flush()
        return None

    # Период платежа знает СТРОКА расхода (окно «Новый платёж» его и спрашивает), а
    # дебиторка до сих пор рождалась без него — отсюда и приходилось угадывать месяц по дате
    # операции. Переносим: без периода помесячное признание не знает, за что платили, а
    # сверка не может сказать, когда ждать закрывающий документ.
    period = await _expense_line_period(session, transaction)
    # Платёж пришёл выпиской, а не черновиком — период знает оплаченный счёт.
    bill_period = None if period else await _paid_bill_period(session, transaction, remainder)
    if bill_period is not None:
        period = (bill_period[0], bill_period[1], None, False)
    if existing is None:
        prepayment = SupplierPrepayment(
            counterparty_id=transaction.counterparty_id,
            kind="subscription",
            wallet_id=transaction.wallet_id,
            amount=remainder,
            amount_settled=Decimal("0.00"),
            status="open",
            cashflow_transaction_id=transaction.id,
            article_id=transaction.article_id,
            service_period_start=period[0] if period else None,
            service_period_end=period[1] if period else None,
            service_period_status="ready" if period else "missing",
            service_period_months=period[2] if period else None,
            auto_recognize_monthly=period[3] if period else False,
            note=(
                "Автопредоплата из банковского списания (предоплатная модель)"
                if transaction.source_kind == "bank_operation"
                else "Автопредоплата из платежа поставщику (предоплатная модель)"
            ),
        )
        session.add(prepayment)
        await session.flush()
        # Разовый контрагент (ремонт, юрист, типография): заплатили и получили — ни периода,
        # ни ожидания документа. Дебиторка здесь фикция, и держать её в очереди признания
        # значит требовать от человека решения по долгу, которого нет. Признаём расход сразу
        # датой платежа; запись предоплаты остаётся закрытой — она несёт связь с деньгами.
        from app.services import subscription_accruals as _subs

        if await _one_off_counterparty(session, transaction.counterparty_id):
            await _subs.recognize_one_off_payment(
                session, prepayment, as_of=transaction.operation_date
            )
        return prepayment

    # Адаптируем существующую нетронутую предоплату НА МЕСТЕ (id сохраняется): при отсутствии
    # открытой КЗ поведение совпадает с прежним (предоплата на всю сумму проводки).
    existing.counterparty_id = transaction.counterparty_id
    existing.amount = remainder
    existing.article_id = transaction.article_id
    existing.wallet_id = transaction.wallet_id
    await session.flush()
    return existing


async def manual_payment_money_is_free(
    session: AsyncSession,
    transaction: CashflowTransaction,
    *,
    origin_source_kind: str | None = None,
) -> bool:
    """Свободны ли деньги проводки для правила 1 — критерий один для всех ручных дверей.

    Проверять НУЖНО по исходной проводке до раскладки сплита: доли рождаются без аллокаций и
    с новым ``source_kind``, поэтому каждая по себе выглядит свободной, даже когда исходный
    платёж давно разложен по документам. Обоснование каждого условия — в
    ``sync_manual_payment_receivable``.
    """
    origin = origin_source_kind or transaction.source_kind
    if origin in SELF_SETTLING_SOURCE_KINDS or origin in DEDICATED_MONEY_SOURCE_KINDS:
        return False
    if transaction.source_kind in SELF_SETTLING_SOURCE_KINDS:
        return False

    own_kind = await session.scalar(
        select(SupplierPrepayment.kind).where(
            SupplierPrepayment.cashflow_transaction_id == transaction.id
        )
    )
    if own_kind is not None:
        # Своя дебиторка правила 1 — его зачёты тоже его, пересобрать вправе. Чужой вид записи
        # (целевой аванс, ДЗ оплаченного счёта) закрывает дверь.
        return own_kind == RULE1_PREPAYMENT_KIND
    allocated = await session.scalar(
        select(func.count(InvoicePaymentAllocation.id)).where(
            InvoicePaymentAllocation.cashflow_transaction_id == transaction.id
        )
    )
    return not allocated


async def sync_manual_payment_receivable(
    session: AsyncSession,
    transaction: CashflowTransaction,
    *,
    origin_source_kind: str | None = None,
    money_is_free: bool | None = None,
) -> SupplierPrepayment | None:
    """Правило 1 для РУЧНОГО разбора ДДС — только по деньгам, которыми никто ещё не распорядился.

    Банковский разбор пускает правило 1 не на всё подряд, а лишь на СВОБОДНЫЕ строки сплита
    (``classifier``: не аванс, без явной ссылки на накладную, не выплата сотруднику). Ручной
    разбор наличных обязан держать ту же границу, иначе перекладывает чужие деньги:

    • проводка адресного контура (оплата счёта, целевой резерв, явная предоплата) — её платёж
      уже привязан к конкретному документу или договору, а FIFO перевесил бы его на посторонний;
    • деньги, уже разложенные аллокациями, — второй заход профинансировал бы документов больше,
      чем в проводке рублей (и снял бы оплату, которую вернуть нечем: складскую накладную FIFO
      правила 1 не подбирает);
    • самооплатный кассовый чек — товар получен на месте, дебиторки быть не может. При сплите
      доли получают ``source_kind='manual_split'``, поэтому самооплатность наследуется от
      РОДИТЕЛЬСКОЙ проводки через ``origin_source_kind``.

    Исключение для аллокаций — проводка, у которой уже есть СВОЯ дебиторка правила 1: значит
    зачёты на ней тоже его, и пересобрать/снять их он вправе (иначе смена контрагента оставила
    бы фантом на прежнем). Известное ограничение: если платёж целиком ушёл в зачёт КЗ и своей
    ДЗ не осталось, последующее исключение проводки зачёт не снимет — деньги придётся
    отвязать вручную.

    ``money_is_free`` передают, когда свобода уже посчитана по родительской проводке (сплит);
    иначе считается здесь по самой проводке.
    """
    is_free = money_is_free
    if is_free is None:
        is_free = await manual_payment_money_is_free(
            session, transaction, origin_source_kind=origin_source_kind
        )
    if not is_free:
        return None
    return await ensure_prepayment_from_bank_transaction(session, transaction)


async def refund_counterparty_prepayments(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    amount: Decimal,
) -> Decimal:
    """Возврат денег от поставщика: погасить его открытые предоплаты (FIFO).

    Возвращает зачтённую сумму; она может быть МЕНЬШЕ ``amount`` — излишек остаётся
    обычным приходом (возврат бывает и не по предоплате). Полностью возвращённая
    запись получает статус ``refunded`` (гард гашения накладными её уже исключает);
    частично возвращённая — увеличенный ``amount_settled`` (остаток дебиторки падает).
    Без commit — вызывающая ручка коммитит всю операцию целиком.
    """
    remaining = _money(amount)
    settled_total = Decimal("0.00")
    if remaining <= 0:
        return settled_total
    rows = await session.scalars(
        select(SupplierPrepayment)
        .where(
            SupplierPrepayment.counterparty_id == counterparty_id,
            SupplierPrepayment.status.in_(OPEN_PREPAYMENT_STATUSES),
            # ДЗ по оплаченному счёту (prepaid_bill) возврат не гасит: её amount_settled должен
            # приходить ТОЛЬКО от зачётов закрывающих (у них есть аллокации, откатываемые _unwind);
            # иначе возврат раздул бы settled без аллокации → _unwind не снял бы, а усадка amount
            # к оплате нарушила бы CHECK amount>=amount_settled. Возврат средств поставщика гасит
            # обычные предоплаты, а излишек остаётся приходом (см. докстринг).
            SupplierPrepayment.kind != BILL_PREPAYMENT_KIND,
        )
        .order_by(SupplierPrepayment.created_at)
    )
    for prepayment in rows.all():
        if remaining <= 0:
            break
        rest = _money(prepayment.amount) - _money(prepayment.amount_settled)
        if rest <= 0:
            continue
        take = min(rest, remaining)
        _consume_prepayment(prepayment, take, full_status="refunded")
        remaining -= take
        settled_total += take
    if settled_total > 0:
        await session.flush()
    return settled_total


async def resync_counterparty_refunds(
    session: AsyncSession, counterparty_id: uuid.UUID | None
) -> None:
    """Привести зачёты возвратов контрагента в соответствие с его проводками-возвратами.

    Возврат денег от поставщика гасит его дебиторку (``refund_counterparty_prepayments``), но —
    в отличие от гашения накладной — делает это БЕЗ аллокации: просто растит ``amount_settled``.
    Поэтому у зачёта не было обратного хода. ``refund_...`` зовётся ровно один раз, при СОЗДАНИИ
    прихода, и последующая переразметка проводки в учёте не отражалась: сняли возвратную статью
    или перевесили приход на другого контрагента — дебиторка оставалась списанной навсегда;
    наоборот, пометили обычный приход возвратной статьёй — зачёт не применялся вовсе.

    Здесь зачёт пересобирается из фактов. «Сколько всего вернул контрагент» берётся из его
    проводок с возвратной статьёй, а ``amount_settled`` каждой предоплаты сбрасывается до её
    АЛЛОКАЦИОННОЙ части (гашения накладными хранятся строками — их трогать нельзя) и затем
    добирается возвратами по FIFO, как это делает сам ``refund_counterparty_prepayments``.
    Идемпотентно: повторный вызов на неизменных данных ничего не меняет. Без commit.

    ДЗ по оплаченному счёту (``prepaid_bill``) исключена — её ``amount_settled`` обязан приходить
    ТОЛЬКО от зачётов закрывающих (см. гард в ``refund_counterparty_prepayments``).
    """
    if counterparty_id is None:
        return
    prepayments = list(
        (
            await session.scalars(
                select(SupplierPrepayment)
                .where(
                    SupplierPrepayment.counterparty_id == counterparty_id,
                    SupplierPrepayment.kind != BILL_PREPAYMENT_KIND,
                )
                .order_by(SupplierPrepayment.created_at)
            )
        ).all()
    )
    if not prepayments:
        return
    # Мягко исключённая проводка выпадает из баланса кошелька — значит и дебиторку гасить не
    # должна, иначе возврат «действует» деньгами, которых в учёте нет.
    remaining = _money(
        await session.scalar(
            select(func.coalesce(func.sum(CashflowTransaction.amount), 0))
            .join(DdsArticle, DdsArticle.id == CashflowTransaction.article_id)
            .where(
                CashflowTransaction.counterparty_id == counterparty_id,
                CashflowTransaction.direction == "in",
                CashflowTransaction.quality_status != EXCLUDED_QUALITY,
                DdsArticle.code == SUPPLIER_REFUND_ARTICLE_CODE,
            )
        )
    )
    for prepayment in prepayments:
        allocated = _money(
            await session.scalar(
                select(func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0)).where(
                    InvoicePaymentAllocation.prepayment_id == prepayment.id
                )
            )
        )
        amount = _money(prepayment.amount)
        # Сброс к аллокационной части: всё, что сверх неё, — след прежнего распределения возвратов.
        prepayment.amount_settled = allocated
        if allocated <= 0:
            prepayment.status = "open"
        elif allocated >= amount:
            prepayment.status = "settled"
        else:
            prepayment.status = "partially_settled"
        rest = amount - allocated
        if rest > 0 and remaining > 0:
            take = min(rest, remaining)
            _consume_prepayment(prepayment, take, full_status="refunded")
            remaining -= take
    await session.flush()


async def cancel_supplier_prepayment(session: AsyncSession, prepayment_id: uuid.UUID) -> None:
    """Снять ошибочно заведённую предоплату вместе с её денежным фактом.

    Только пока дебиторка не тронута: ни рубля не зачтено накладными. Удаляется
    и запись, и породившая её out-CashflowTransaction (баланс кошелька
    восстанавливается). Гашеную предоплату отменить нельзя — сначала снимают
    аллокации.
    """
    prepayment = await session.get(SupplierPrepayment, prepayment_id)
    if prepayment is None:
        raise CounterpartyPaymentError("Предоплата не найдена")
    if _money(prepayment.amount_settled) > 0 or prepayment.status != "open":
        raise CounterpartyPaymentError(
            "Предоплата уже начала гаситься накладными — отмена невозможна"
        )
    allocation_id = await session.scalar(
        select(InvoicePaymentAllocation.id)
        .where(InvoicePaymentAllocation.prepayment_id == prepayment.id)
        .limit(1)
    )
    if allocation_id is not None:
        raise CounterpartyPaymentError(
            "Предоплата уже начала гаситься накладными — отмена невозможна"
        )
    if prepayment.cashflow_transaction_id is not None:
        # Cash-зачёты кредиторки (правило 1) этой проводки снимаем до удаления проводки —
        # иначе FK SET NULL осиротит аллокацию и накладная останется «оплаченной». Замороженный
        # в банк-черновике зачёт снять нельзя — блокируем отмену до отката черновика.
        if not await _unwind_transaction_kz_settlements(
            session, prepayment.cashflow_transaction_id, include_bills=True
        ):
            raise CounterpartyPaymentError(
                "Платёж уже погасил кредиторку, отправленную в банк-черновик — "
                "сначала откатите черновик"
            )
        transaction = await session.get(CashflowTransaction, prepayment.cashflow_transaction_id)
        if transaction is not None:
            await session.delete(transaction)
    await session.delete(prepayment)
    await session.commit()


async def settle_invoice_from_prepayment(
    session: AsyncSession,
    *,
    invoice_id: uuid.UUID,
    prepayment_id: uuid.UUID,
    amount: Decimal | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> SupplierInvoice:
    """Погасить (часть) накладной против остатка ранее выданной предоплаты. Денег не двигает."""
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise CounterpartyPaymentError("Накладная не найдена")
    if invoice.payment_status == "void":
        raise CounterpartyPaymentError("Накладная аннулирована")
    if invoice.direction != "payable":
        raise CounterpartyPaymentError("Доходную накладную нельзя гасить из предоплаты")
    if invoice.barter_role is not None:
        raise CounterpartyPaymentError("Бартерную накладную нельзя гасить из предоплаты")

    prepayment = await session.get(SupplierPrepayment, prepayment_id)
    if prepayment is None:
        raise CounterpartyPaymentError("Предоплата не найдена")
    if prepayment.counterparty_id != invoice.counterparty_id:
        raise CounterpartyPaymentError("Предоплата и накладная относятся к разным контрагентам")
    if prepayment.status not in OPEN_PREPAYMENT_STATUSES:
        # Возвращённую/закрытую предоплату (например 'refunded' со стейл amount_settled<amount)
        # гасить нельзя — иначе списали бы остаток уже не существующей дебиторки.
        raise CounterpartyPaymentError("Предоплата недоступна для гашения (возвращена или закрыта)")

    inv_remaining = await _invoice_remaining(session, invoice)
    pre_remaining = _money(prepayment.amount) - _money(prepayment.amount_settled)
    if inv_remaining <= 0:
        raise CounterpartyPaymentError("Накладная уже оплачена")
    if pre_remaining <= 0:
        raise CounterpartyPaymentError("Предоплата исчерпана")

    alloc = _money(amount) if amount is not None else min(inv_remaining, pre_remaining)
    alloc = min(alloc, inv_remaining, pre_remaining)
    if alloc <= 0:
        raise CounterpartyPaymentError("Сумма гашения вне допустимого остатка")

    await _allocate_invoice_from_prepayment(
        session, invoice=invoice, prepayment=prepayment, amount=alloc, actor_user_id=actor_user_id
    )
    await _recompute_status(session, invoice)
    await session.commit()
    await session.refresh(invoice)
    return invoice


def _prefer_matching_period(
    prepayments: Sequence[SupplierPrepayment], invoice: SupplierInvoice
) -> list[SupplierPrepayment]:
    """Порядок зачёта: сначала предоплаты ЗА ТОТ ЖЕ период, что и документ, потом остальные.

    Суммы это не меняет — меняет, ЧЬЮ дебиторку закроет документ. Без этого акт за июль гасит
    самый старый открытый платёж, даже если тот был за 2 квартал: деньги за апрель-июнь
    оказываются закрыты июльской бумагой, а помесячное признание за апрель потом не находит,
    что гасить, и заводит расход повторно.

    Порядок, а не жёсткий фильтр: если совпадающих по периоду не хватило, документ по-прежнему
    гасится любой открытой предоплатой — иначе зачёт просто перестал бы работать там, где
    периодов нет (на проде это почти все платежи).
    """
    if invoice.service_period_start is None or invoice.service_period_end is None:
        return list(prepayments)

    def overlaps(prepayment: SupplierPrepayment) -> bool:
        start = prepayment.service_period_start
        end = prepayment.service_period_end
        if start is None or end is None:
            return False
        return start <= invoice.service_period_end and end >= invoice.service_period_start

    matching = [item for item in prepayments if overlaps(item)]
    rest = [item for item in prepayments if not overlaps(item)]
    return matching + rest


async def auto_settle_invoice_from_open_prepayments(
    session: AsyncSession,
    invoice: SupplierInvoice,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> Decimal:
    """Авто-гашение финансового закрывающего документа из предоплат (FIFO), без коммита.

    Кейс владельца (2026-07-16): поставщик оплачивается авансом, закрывающий УПД из ЭДО
    не должен попадать «к оплате» — он гасит дебиторку. Деньги не двигаются (ушли при
    создании предоплаты). Возвращает суммарно погашенное (0 — если предоплат нет).

    Складские накладные независимо от вида предоплаты НЕ трогаем: конкретную поставку оператор
    связывает с авансом явно через ``settle_invoice_from_prepayment``. У финансовых документов
    дополнительно не трогаем целевые товарные авансы (kind='goods')."""
    if invoice.operational_scope != AUTO_SETTLEMENT_OPERATIONAL_SCOPE:
        return Decimal("0.00")
    if invoice.draft_id is not None:
        # Документ отправлен в банк — реальный платёж в пути. Гасить его зачётом из предоплаты
        # сейчас = закрыть дважды: зачётом И платежом, который вот-вот исполнится (поставщику
        # уйдут лишние деньги). Зачёт возможен только для документов вне банковских черновиков.
        return Decimal("0.00")
    total = Decimal("0.00")
    prepayments = (
        await session.scalars(
            select(SupplierPrepayment)
            .where(
                SupplierPrepayment.counterparty_id == invoice.counterparty_id,
                SupplierPrepayment.status.in_(OPEN_PREPAYMENT_STATUSES),
                SupplierPrepayment.kind.notin_(EARMARKED_PREPAYMENT_KINDS),
            )
            .order_by(SupplierPrepayment.created_at)
        )
    ).all()
    for prepayment in _prefer_matching_period(prepayments, invoice):
        inv_remaining = await _invoice_remaining(session, invoice)
        if inv_remaining <= 0:
            break
        pre_remaining = _money(prepayment.amount) - _money(prepayment.amount_settled)
        if pre_remaining <= 0:
            continue
        alloc = min(inv_remaining, pre_remaining)
        await _allocate_invoice_from_prepayment(
            session,
            invoice=invoice,
            prepayment=prepayment,
            amount=alloc,
            actor_user_id=actor_user_id,
        )
        total += alloc
    if total > 0:
        await _recompute_status(session, invoice)
    return total


async def apply_closing_document(
    session: AsyncSession,
    invoice: SupplierInvoice,
    *,
    actor_user_id: uuid.UUID | None = None,
    as_of: date | None = None,
) -> Decimal:
    """Провести закрывающий документ (УПД/акт/приходную/чек) по канону ДЗ/КЗ (владелец 17.07).

    Счёт (doc_kind='bill') в баланс ДЗ/КЗ не входит — для него это no-op (только фиксируем
    activation_status='active', в контуре он не участвует).

    Для закрывающего (doc_kind='closing') — «факт выполненных работ»:
      • дата документа В БУДУЩЕМ (правило 4: ЭкоЦентр шлёт УПД июля датой 31.07) →
        activation_status='pending', обязательство пока НЕ создаём, дебиторку не гасим;
        ночная джоба ``activate_due_closing_invoices`` проведёт его в свою дату;
      • иначе активируем сразу; финансовый УПД/акт FIFO-гасит открытую дебиторку, а товарная
        накладная остаётся отдельной КЗ до ручного зачёта конкретного аванса.
    Денег не двигает (они ушли при создании предоплаты). Возвращает погашенное авансами. Без
    коммита."""
    if invoice.doc_kind != "closing":
        invoice.activation_status = "active"
        return Decimal("0.00")
    today = as_of or datetime.now(MOSCOW_TZ).date()
    if invoice.invoice_date is not None and invoice.invoice_date > today:
        invoice.activation_status = "pending"
        return Decimal("0.00")
    invoice.activation_status = "active"
    # Импорт локальный: оба модуля зовут этот, на верхнем уровне вышел бы цикл.
    from app.services import service_agreement_accruals, subscription_accruals

    # У контрагента с договором услуги источник истины — договор, а не бумага (решение
    # владельца 01.08.2026). Пришедший акт регистрируем, но учёт им не двигаем: иначе он
    # отменил бы наше начисление и переписал расход месяца суммой, которую мы не заказывали.
    # ИП Наумченко 28.07.2026 прислала акт впервые за всё время — по договору он информация.
    if invoice.source != subscription_accruals.SELF_BILLED_SOURCE:
        period_anchor = invoice.service_period_end or invoice.invoice_date or today
        if await service_agreement_accruals.covered_by_agreement(
            session,
            invoice.counterparty_id,
            on=period_anchor,
            article_id=invoice.dds_article_id,
        ):
            invoice.informational = True
            return Decimal("0.00")

    # Настоящий документ сильнее самоакта: если месяц уже был признан внутренним
    # ``self_billed`` (контрагент документов не присылал), тот аннулируется и возвращает
    # дебиторку — иначе период закрылся бы дважды и расход в P&L удвоился.
    await subscription_accruals.supersede_self_billed(session, invoice)
    return await auto_settle_invoice_from_open_prepayments(
        session, invoice, actor_user_id=actor_user_id
    )


async def activate_due_closing_invoices(
    session: AsyncSession, *, as_of: date | None = None, commit: bool = True
) -> dict[str, int]:
    """Правило 4: закрывающие документы с наступившей ДАТОЙ ДОКУМЕНТА вступают в силу.

    Будущий УПД (activation_status='pending') в свою дату активируется: гасит открытую
    дебиторку контрагента FIFO, остаток становится кредиторкой. ``invoice_date <= today``
    (в свою дату уже действует). Идемпотентно: после активации статус 'active', повторно
    джоба его не берёт. Возвращает счётчики для лога.

    Проводит документ ТЕМ ЖЕ ``apply_closing_document``, что и все двери приёма. Пока джоба
    сама ставила ``active`` и звала только гашение авансов, будущий документ навсегда минул
    обе проверки, которые ``apply_closing_document`` делает до гашения: не проверялся договор
    (флаг ``informational``) и не аннулировался самоакт (``supersede_self_billed``). Один и тот
    же УПД учитывался по-разному в зависимости от того, вчерашней он датой или завтрашней: акт
    Микроэля на 9 000 ₽ за апрель-июнь, пришедший датой 30.06, оставлял апрель и май
    признанными дважды — 6 000 ₽ лишнего расхода."""
    today = as_of or datetime.now(MOSCOW_TZ).date()
    from app.services import supplier_service_periods
    rows = list(
        (
            await session.scalars(
                select(SupplierInvoice)
                .where(
                    SupplierInvoice.doc_kind == "closing",
                    SupplierInvoice.activation_status == "pending",
                    SupplierInvoice.payment_status != "void",
                    SupplierInvoice.invoice_date.is_not(None),
                    SupplierInvoice.invoice_date <= today,
                )
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    settled_count = 0
    for invoice in rows:
        settled = await apply_closing_document(session, invoice, as_of=today)
        # Документ мог оказаться информационным (у контрагента договор) — тогда заведённое
        # при приёме начисление снимается, иначе месяц был бы признан и договором, и актом.
        await supplier_service_periods.sync_invoice_accrual(session, invoice)
        if settled > 0:
            settled_count += 1
    if commit:
        await session.commit()
    else:
        await session.flush()
    return {"activated": len(rows), "settled_from_prepayments": settled_count}


async def counterparty_prepayment_balance(
    session: AsyncSession, counterparty_id: uuid.UUID
) -> Decimal:
    """Остаток выданных предоплат контрагенту (= «поставщик нам должен»)."""
    total = await session.scalar(
        select(
            func.coalesce(
                func.sum(SupplierPrepayment.amount - SupplierPrepayment.amount_settled), 0
            )
        )
        .where(SupplierPrepayment.counterparty_id == counterparty_id)
        .where(SupplierPrepayment.status.in_(OPEN_PREPAYMENT_STATUSES))
    )
    return _money(total)

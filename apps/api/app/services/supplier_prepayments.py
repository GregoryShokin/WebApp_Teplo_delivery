"""Предоплаты поставщикам (дебиторка): мы платим вперёд — поставщик должен привезти.

Создание предоплаты = реальный расход денег (out-CashflowTransaction, source_kind=
'supplier_prepayment'), обычно с кошелька «Сейф». Гашение приходящих payable-накладных —
через InvoicePaymentAllocation(source_kind='prepayment'), которая денег НЕ двигает (они
ушли при создании предоплаты). Отдельный учёт от кредиторки и товарного бартера.
"""

from __future__ import annotations

import uuid
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
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierPrepayment,
    Wallet,
)
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

# Целевые авансы под конкретную поставку (kind='goods') гасятся ЯВНО — когда придёт накладная
# именно этой поставки (settle_invoice_from_prepayment). Их НЕЛЬЗЯ авто-гасить FIFO любой
# приходящей накладной: иначе аванс под недопоставленный заказ молча «съест» посторонний
# счёт/УПД. Остальные виды (subscription/ad/rent/other) — это «деньги у поставщика» (баланс
# рекламного кабинета/подписки), который закрывающие документы правомерно списывают по FIFO.
EARMARKED_PREPAYMENT_KINDS = frozenset({"goods"})

# ДЗ из оплаты счёта (doc_kind='bill'). Канон владельца 17.07: «Сам по себе счёт ничего не делает…
# Оплата счёта уходит в дебиторскую задолженность». Счёт — не обязательство (в КЗ не входит), но
# его ОПЛАТА — предоплата поставщику: деньги ушли, закрывающего документа ещё нет. Закрывающий
# УПД/акт затем гасит эту ДЗ по правилу 2 (auto_settle). Kind НЕ входит в
# EARMARKED_PREPAYMENT_KINDS, иначе закрывающий документ не смог бы её авто-погасить и мы
# получили бы фантомную КЗ (блокер-2).
BILL_PREPAYMENT_KIND = "prepaid_bill"


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


async def _bill_paid_already_receivable(
    session: AsyncSession, invoice: SupplierInvoice
) -> Decimal:
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
    """Правило 2 в обратном порядке: открытые закрывающие документы контрагента гасятся его
    открытыми предоплатами (FIFO по дате документа). Используется, когда предоплата (напр. от
    оплаты счёта) появилась ПОЗЖЕ уже висящей кредиторки. Закрывающие с doc_kind='closing' при
    гашении зовут ``_recompute_status`` — он рекурсию в bill-чокпоинт не даёт (это не счёт)."""
    closings = (
        await session.scalars(
            select(SupplierInvoice)
            .where(
                SupplierInvoice.counterparty_id == counterparty_id,
                SupplierInvoice.direction == "payable",
                SupplierInvoice.doc_kind == "closing",
                SupplierInvoice.activation_status == "active",
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
    """Правило 1: деньги транзакции FIFO-гасят открытую кредиторку контрагента.

    Открытая КЗ = неоплаченный остаток АКТИВНЫХ закрывающих документов (doc_kind='closing',
    activation_status='active'), FIFO по дате документа (будущие 'pending' УПД не трогаем —
    их ещё нет как обязательства). Аллокация source_kind='cash' денег НЕ двигает (они ушли
    транзакцией). Возвращает погашенную сумму (≤ limit)."""
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
                SupplierInvoice.activation_status == "active",
                SupplierInvoice.barter_role.is_(None),
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
        closing_ids = {inv.id for inv in invoices if inv.doc_kind == "closing"}
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
    у чьих счетов prepaid_bill-записи нет."""
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
            | (InvoicePaymentAllocation.bank_operation_id.in_(operation_ids)),
            ~own_prepaid,
        )
    )
    return _money(total)


async def ensure_prepayment_from_bank_transaction(
    session: AsyncSession, transaction: CashflowTransaction
) -> SupplierPrepayment | None:
    """Синхронизировать распределение банк-платежа с состоянием проводки (правило 1 канона).

    Канон (владелец 17.07): «контрагентов НЕ классифицируем, правила универсальны». ЛЮБОЙ
    свободный банк-платёж поставщику (не через счёт-черновик — те короткозамыкаются на pre-booked
    ДО этого хука) сначала ГАСИТ его открытую кредиторку (неоплаченные АКТИВНЫЕ закрывающие
    документы) FIFO, а ИЗЛИШЕК становится дебиторкой (предоплатой). Так закрывается контур и для
    ПОСТОПЛАТНЫХ поставщиков (Стартер/«Назад в будущее»): УПД создаёт КЗ → платёж её гасит.
    Ограничение — только поставщики (есть payable-профиль); у сотрудников/налоговой/банков его нет,
    их платежи сюда не попадают. Флаг ``bank_payments_create_prepayment`` контур больше НЕ гейтит.
    Новую ДДС-проводку НЕ создаём — деньги уже учтены транзакцией выписки; движем только зачёты КЗ
    (cash-аллокации, денег не двигают) и запись предоплаты.

    Не «создать один раз», а ПРИВЕСТИ распределение к текущей проводке — переклассификация
    не должна оставлять фантом:
      • транзакция больше не квалифицируется (сняли контрагента / стала приходом / контрагент
        без payable-профиля) → снимаем нетронутую предоплату И её зачёты КЗ;
      • контрагент/сумма/кошелёк изменились → пересобираем зачёты + предоплату;
      • гашёную предоплату (amount_settled>0) или замороженные зачёты (накладная в черновике)
        не трогаем — историю сохраняем.
    Идемпотентно: одна транзакция = один набор зачётов КЗ + одна предоплата на остаток."""
    existing = await session.scalar(
        select(SupplierPrepayment).where(
            SupplierPrepayment.cashflow_transaction_id == transaction.id
        )
    )
    should_have = transaction.direction == "out" and transaction.counterparty_id is not None
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
    if not await _unwind_transaction_kz_settlements(
        session, transaction.id, include_bills=False
    ):
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
    settled = await _settle_open_kz_from_transaction(
        session, transaction, limit=amount - consumed
    )
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
            note="Автопредоплата из банковского списания (предоплатная модель)",
        )
        session.add(prepayment)
        await session.flush()
        return prepayment

    # Адаптируем существующую нетронутую предоплату НА МЕСТЕ (id сохраняется): при отсутствии
    # открытой КЗ поведение совпадает с прежним (предоплата на всю сумму проводки).
    existing.counterparty_id = transaction.counterparty_id
    existing.amount = remainder
    existing.article_id = transaction.article_id
    existing.wallet_id = transaction.wallet_id
    await session.flush()
    return existing


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


async def auto_settle_invoice_from_open_prepayments(
    session: AsyncSession,
    invoice: SupplierInvoice,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> Decimal:
    """Авто-гашение счёта из ОТКРЫТЫХ предоплат контрагента (FIFO), без коммита.

    Кейс владельца (2026-07-16): поставщик оплачивается авансом, закрывающий УПД из ЭДО
    не должен попадать «к оплате» — он гасит дебиторку. Деньги не двигаются (ушли при
    создании предоплаты). Возвращает суммарно погашенное (0 — если предоплат нет).

    Целевые товарные авансы (kind='goods') НЕ трогаем: они привязаны к конкретной поставке
    и гасятся явно (settle_invoice_from_prepayment), иначе посторонняя накладная списала бы
    аванс под недопоставленный заказ."""
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
    for prepayment in prepayments:
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
      • иначе активируем сразу: FIFO-гасим открытую дебиторку контрагента (правило 2),
        остаток становится кредиторкой.
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
    джоба его не берёт. Возвращает счётчики для лога."""
    today = as_of or datetime.now(MOSCOW_TZ).date()
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
        invoice.activation_status = "active"
        settled = await auto_settle_invoice_from_open_prepayments(session, invoice)
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

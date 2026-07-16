"""Предоплаты поставщикам (дебиторка): мы платим вперёд — поставщик должен привезти.

Создание предоплаты = реальный расход денег (out-CashflowTransaction, source_kind=
'supplier_prepayment'), обычно с кошелька «Сейф». Гашение приходящих payable-накладных —
через InvoicePaymentAllocation(source_kind='prepayment'), которая денег НЕ двигает (они
ушли при создании предоплаты). Отдельный учёт от кредиторки и товарного бартера.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CashflowTransaction,
    Counterparty,
    CounterpartyPayableProfile,
    DdsArticle,
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierPrepayment,
    Wallet,
)
from app.services.counterparty_matching import _invoice_remaining, _recompute_status
from app.services.counterparty_payments import CounterpartyPaymentError

PREPAYMENT_ARTICLE_CODE = "advance_to_supplier"
# Приходная статья «Возврат переплаты от поставщиков» — возврат гасит открытые предоплаты.
SUPPLIER_REFUND_ARTICLE_CODE = "vozvrat_pereplaty_ot_postavschikov"
OPEN_PREPAYMENT_STATUSES = ("open", "partially_settled")


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


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


async def ensure_prepayment_from_bank_transaction(
    session: AsyncSession, transaction: CashflowTransaction
) -> SupplierPrepayment | None:
    """Предоплатная модель по банк-фиду: списание в пользу контрагента → дебиторка.

    Вызывается классификатором ДДС после присвоения контрагента. Новую ДДС-проводку
    НЕ создаёт (деньги уже учтены транзакцией из выписки) — только запись предоплаты,
    привязанную к транзакции. Идемпотентно: одна предоплата на одну транзакцию.
    Гасится закрывающими УПД из СБИС (auto-settle при материализации)."""
    if transaction.direction != "out" or transaction.counterparty_id is None:
        return None
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == transaction.counterparty_id
        )
    )
    if profile is None or not profile.bank_payments_create_prepayment:
        return None
    existing = await session.scalar(
        select(SupplierPrepayment).where(
            SupplierPrepayment.cashflow_transaction_id == transaction.id
        )
    )
    if existing is not None:
        return existing
    prepayment = SupplierPrepayment(
        counterparty_id=transaction.counterparty_id,
        kind="subscription",
        wallet_id=transaction.wallet_id,
        amount=_money(transaction.amount),
        amount_settled=Decimal("0.00"),
        status="open",
        cashflow_transaction_id=transaction.id,
        article_id=transaction.article_id,
        note="Автопредоплата из банковского списания (предоплатная модель)",
    )
    session.add(prepayment)
    await session.flush()
    return prepayment


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
        prepayment.amount_settled = _money(prepayment.amount_settled) + take
        if prepayment.amount_settled >= _money(prepayment.amount):
            prepayment.status = "refunded"
        remaining -= take
        settled_total += take
    if settled_total > 0:
        await session.flush()
    return settled_total


async def prepayment_remaining(prepayment: SupplierPrepayment) -> Decimal:
    return _money(prepayment.amount) - _money(prepayment.amount_settled)


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

    session.add(
        InvoicePaymentAllocation(
            invoice_id=invoice.id,
            source_kind="prepayment",
            prepayment_id=prepayment.id,
            amount=alloc,
            created_by_user_id=actor_user_id,
        )
    )
    prepayment.amount_settled = _money(prepayment.amount_settled) + alloc
    prepayment.status = (
        "settled" if prepayment.amount_settled >= _money(prepayment.amount) else "partially_settled"
    )
    await session.flush()
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
    создании предоплаты). Возвращает суммарно погашенное (0 — если предоплат нет)."""
    total = Decimal("0.00")
    prepayments = (
        await session.scalars(
            select(SupplierPrepayment)
            .where(
                SupplierPrepayment.counterparty_id == invoice.counterparty_id,
                SupplierPrepayment.status.in_(OPEN_PREPAYMENT_STATUSES),
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
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id,
                source_kind="prepayment",
                prepayment_id=prepayment.id,
                amount=alloc,
                created_by_user_id=actor_user_id,
            )
        )
        prepayment.amount_settled = _money(prepayment.amount_settled) + alloc
        prepayment.status = (
            "settled"
            if prepayment.amount_settled >= _money(prepayment.amount)
            else "partially_settled"
        )
        await session.flush()
        total += alloc
    if total > 0:
        await _recompute_status(session, invoice)
    return total


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

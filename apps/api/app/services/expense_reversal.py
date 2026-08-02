"""Откат признанного расхода — целиком или частью.

ЗАЧЕМ. Признание не бывает безошибочным: период указали шире, чем услуга реально оказана;
контрагент вернул часть денег; лицензию отключили в середине месяца. Пока откатить было нечем,
единственным выходом оставалась правка сумм в базе руками — то есть расход в P&L, который никто
не может объяснить.

ЧТО ПРОИСХОДИТ. Сумма уходит из расхода и возвращается туда, откуда пришла, — в дебиторку:
признали 3 000 ₽, откатили 1 000 → в прибыли месяца осталось 2 000, а 1 000 снова «нам должны
закрыть документами или вернуть». Деньги при этом никуда не движутся: платёж как был, так и
остался, меняется только то, чем он считается.

ЧТО ОТКАТИТЬ НЕЛЬЗЯ. Признание по документу контрагента (УПД, акт). Там сумма расхода — не наше
решение, а первичка: если услуга оказана не на всю сумму, контрагент выставляет корректировочный
документ, и расход меняется вместе с ним. Откатывать можно только то, что система посчитала
сама: самоакты из предоплаты и начисления по договору услуги.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    InvoicePaymentAllocation,
    SupplierExpenseAccrual,
    SupplierExpenseReversal,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services import supplier_service_periods as periods
from app.services.subscription_accruals import SELF_BILLED_SOURCE

__all__ = ["ReversalRefused", "reverse_expense"]


class ReversalRefused(ValueError):
    """Откатить этот расход нельзя — с объяснением, почему."""


async def _self_billed_invoice(
    session: AsyncSession, accrual: SupplierExpenseAccrual
) -> SupplierInvoice:
    if accrual.invoice_id is None:
        raise ReversalRefused(
            "Этот расход признан строкой платежа, а не документом — откатить его нельзя. "
            "Измените период платежа"
        )
    invoice = await session.get(SupplierInvoice, accrual.invoice_id)
    if invoice is None:
        raise ReversalRefused("Документ признания не найден")
    if invoice.source != SELF_BILLED_SOURCE:
        raise ReversalRefused(
            "Расход признан документом контрагента — его сумму меняет корректировочный "
            "документ, а не откат"
        )
    return invoice


async def reverse_expense(
    session: AsyncSession,
    accrual: SupplierExpenseAccrual,
    *,
    amount: Decimal,
    reason: str,
    actor_user_id: uuid.UUID | None,
) -> SupplierExpenseReversal:
    """Снять часть признанного расхода и вернуть её в дебиторку. Коммитит.

    Порядок важен: сначала уменьшаем гашение (дебиторка возвращается контрагенту в минус
    расчётов), затем сам документ и начисление. Если делать наоборот, промежуточное состояние
    нарушает инвариант «сумма аллокаций не больше суммы документа».
    """
    amount = periods.money(amount)
    if amount <= 0:
        raise ReversalRefused("Сумма отката должна быть больше нуля")
    if not reason.strip():
        raise ReversalRefused("Укажите причину отката — она останется в журнале")
    if accrual.status == "cancelled":
        raise ReversalRefused("Этот расход уже отменён")
    current = periods.money(accrual.amount)
    if amount > current:
        raise ReversalRefused(
            f"Нельзя откатить больше, чем признано: в расходе {current} ₽"
        )

    invoice = await _self_billed_invoice(session, accrual)
    reversal = SupplierExpenseReversal(
        accrual_id=accrual.id,
        amount=amount,
        amount_before=current,
        recognition_month=accrual.recognition_month,
        actor_user_id=actor_user_id,
        reason=reason.strip(),
    )
    session.add(reversal)

    await _release_settlement(session, invoice, amount)

    invoice.amount = periods.money(invoice.amount) - amount
    accrual.amount = current - amount
    if accrual.amount <= 0:
        accrual.status = "cancelled"
        # Документ пустой суммы — не документ, гасим его. А вот ключ идемпотентности
        # (``self:{платёж}:{месяц}`` / ``svc:{договор}:{месяц}``) НЕ освобождаем намеренно:
        # занятый ключ и есть отметка «месяц откачен вручную». Пока ключ снимался, ночная
        # джоба в 00:04 МСК не находила месяц закрытым и признавала его заново — решение
        # владельца жило до ближайшей ночи, и расход возвращался сам собой, без следа в
        # журнале. Признать месяц заново теперь можно только осознанно, через частичный
        # откат или изменение периода.
        invoice.payment_status = "void"
    await session.commit()
    await session.refresh(reversal)
    return reversal


async def _release_settlement(
    session: AsyncSession, invoice: SupplierInvoice, amount: Decimal
) -> None:
    """Вернуть сумму в дебиторку, разбирая гашения этого документа.

    Сначала предоплатные аллокации — именно они и есть «деньги ждут закрытия». Денежные
    (постоплата по договору) трогаем в последнюю очередь и возвращаем отдельной предоплатой:
    удалить такую аллокацию нельзя, иначе живой платёж останется ничьим.
    """
    allocations = list(
        (
            await session.scalars(
                select(InvoicePaymentAllocation)
                .where(InvoicePaymentAllocation.invoice_id == invoice.id)
                .order_by(InvoicePaymentAllocation.prepayment_id.is_(None))
            )
        ).all()
    )
    left = amount
    for allocation in allocations:
        if left <= 0:
            break
        taken = min(periods.money(allocation.amount), left)
        left -= taken
        if allocation.prepayment_id is not None:
            prepayment = await session.get(SupplierPrepayment, allocation.prepayment_id)
            if prepayment is not None:
                prepayment.amount_settled = max(
                    periods.money(prepayment.amount_settled) - taken, Decimal("0.00")
                )
                prepayment.status = (
                    "open"
                    if periods.money(prepayment.amount_settled) <= 0
                    else "partially_settled"
                )
        else:
            # Живые деньги: возвращаем их дебиторкой, иначе платёж повиснет без основания.
            session.add(
                SupplierPrepayment(
                    counterparty_id=invoice.counterparty_id,
                    kind="subscription",
                    amount=taken,
                    amount_settled=Decimal("0.00"),
                    status="open",
                    article_id=invoice.dds_article_id,
                    service_period_start=invoice.service_period_start,
                    service_period_end=invoice.service_period_end,
                    service_period_status="ready"
                    if invoice.service_period_start is not None
                    else "missing",
                )
            )
        rest = periods.money(allocation.amount) - taken
        if rest > 0:
            allocation.amount = rest
        else:
            await session.delete(allocation)
    await session.flush()

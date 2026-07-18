"""Денежное гашение бартерного займа (целевой процесс владельца, 2026-07-18).

Бартерный заём номинирован ТОВАРОМ; деньги — второй способ его закрыть (первый — возвратная
накладная по исходной цене, ``warehouse_invoices.create_barter_return``):

- **Наш заём** (``direction='receivable'``, мы выдали): контрагент платит по ТЕКУЩЕЙ цене —
  оператор вводит кг и цену за кг (система подсказывает последнюю закупочную), деньги приходят
  налом на выбранный кошелёк или банковской операцией. В зачёт долга идёт qty × ИСХОДНАЯ цена
  займа (долг товарный), фактические деньги могут отличаться.
- **Их заём** (``direction='payable'``, нам выдали): платим «как обычную поставку» — сумму по
  накладной, через обычную оплату с кошелька либо привязку банковской операции. Зачёт несут
  аллокации накладной; статус займа синхронизируется от зачётной стоимости.

Денежное гашение склад НЕ двигает (возвратной накладной нет) — строка гашения ссылается на
ДДС-проводку. Авто-контуры (правило 1, авто-сверка, авто-зачёт) бартер по-прежнему исключают:
гашение займа — только явное действие оператора отсюда.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    BankOperation,
    BarterReturnLine,
    CashflowTransaction,
    DdsArticle,
    InvoiceLineItem,
    SupplierInvoice,
    Wallet,
)
from app.services.supplier_prepayments import SUPPLIER_REFUND_ARTICLE_CODE
from app.services.warehouse_invoices import (
    WarehouseInvoiceError,
    _loan_line_returned_qty,
    loan_settled_value,
    sync_barter_loan_status,
)


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _qty(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


@dataclass
class MoneyReturnLine:
    """Гашение нашего займа деньгами: кг по строке займа × ТЕКУЩАЯ цена (ввод оператора)."""

    loan_line_item_id: uuid.UUID
    quantity: Decimal
    unit_price: Decimal


async def _resync_rule1_prepayment(
    session: AsyncSession, bank_operation_id: uuid.UUID
) -> None:
    """Пересинхронизировать ДЗ правила 1 после того, как деньги операции ушли на заём.

    Классификатор уже провёл этот платёж через правило 1: бартерные накладные из FIFO-гашения
    КЗ исключены, поэтому вся сумма стала предоплатой (ДЗ). Явное гашение займа тратит те же
    деньги — без пересчёта предоплата осталась бы фантомной дебиторкой на полную сумму
    (двойной учёт). ``ensure_prepayment_from_bank_transaction`` считает остаток от «бюджета
    платежа» (сумма − уже пристроенное), поэтому после аллокации на заём ужмёт/снимет ДЗ."""
    from app.services.supplier_prepayments import ensure_prepayment_from_bank_transaction

    operation = await session.get(BankOperation, bank_operation_id)
    if operation is None or operation.cashflow_transaction_id is None:
        return
    transaction = await session.get(CashflowTransaction, operation.cashflow_transaction_id)
    if transaction is not None:
        await ensure_prepayment_from_bank_transaction(session, transaction)


async def _assert_operation_free(
    session: AsyncSession, operation: BankOperation, needed: Decimal
) -> None:
    """Операция должна иметь свободный остаток ≥ needed: одни деньги — один долг.

    Гашение НАШЕГО займа аллокаций не создаёт (склад не двигается, ДДС-проводка приходная),
    поэтому занятость считаем по строкам леджера, уже привязанным к этой проводке."""
    from app.services.counterparty_matching import payment_allocated_amount

    used = await payment_allocated_amount(session, bank_operation_id=operation.id)
    if operation.cashflow_transaction_id is not None:
        ledger_used = await session.scalar(
            select(func.coalesce(func.sum(BarterReturnLine.amount), 0)).where(
                BarterReturnLine.cashflow_transaction_id == operation.cashflow_transaction_id
            )
        )
        used += _money(ledger_used)
    free = _money(abs(operation.amount)) - used
    if needed > free:
        raise WarehouseInvoiceError(
            f"Свободный остаток операции {max(free, Decimal('0.00'))} меньше суммы гашения {needed}"
        )


async def _load_loan(session: AsyncSession, loan_id: uuid.UUID) -> SupplierInvoice:
    loan = await session.get(SupplierInvoice, loan_id)
    if loan is None or loan.barter_role != "loan":
        raise WarehouseInvoiceError("Заём не найден")
    if loan.barter_return_status == "returned":
        raise WarehouseInvoiceError("Заём уже полностью возвращён")
    return loan


async def _refund_article_id(session: AsyncSession) -> uuid.UUID:
    article_id = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == SUPPLIER_REFUND_ARTICLE_CODE)
    )
    if article_id is None:
        raise WarehouseInvoiceError("Статья «Возврат переплаты от поставщиков» не найдена")
    return article_id


async def settle_receivable_loan_with_money(
    session: AsyncSession,
    *,
    loan_id: uuid.UUID,
    operation_date: date,
    lines: list[MoneyReturnLine],
    wallet_id: uuid.UUID | None = None,
    bank_operation_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Контрагент гасит НАШ товарный заём деньгами: кг × текущая цена.

    Деньги: нал — приход на выбранный кошелёк (новая ДДС-проводка); банк — входящая операция
    выписки (проводка создаётся/привязывается к ней, операция становится classified). Зачёт
    долга — qty × ИСХОДНАЯ цена строки займа; кг списываются с остатка строки."""
    loan = await _load_loan(session, loan_id)
    if loan.direction != "receivable":
        raise WarehouseInvoiceError("Это их заём нам — гасите его оплатой накладной")
    if not lines:
        raise WarehouseInvoiceError("Укажите, какие позиции гасятся деньгами")
    if (wallet_id is None) == (bank_operation_id is None):
        raise WarehouseInvoiceError("Выберите ровно один канал денег: кошелёк или банк-операцию")

    loan_lines = {
        line.id: line
        for line in (
            await session.scalars(
                select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == loan.id)
            )
        ).all()
    }
    returned_qty = await _loan_line_returned_qty(session, loan.id)

    money_total = Decimal("0.00")
    credited_total = Decimal("0.00")
    resolved: list[tuple[InvoiceLineItem, Decimal, Decimal]] = []
    for item in lines:
        line = loan_lines.get(item.loan_line_item_id)
        if line is None:
            raise WarehouseInvoiceError("Строка займа не найдена")
        qty = _qty(item.quantity)
        price = _money(item.unit_price)
        if qty <= 0 or price <= 0:
            raise WarehouseInvoiceError(f"Количество и цена по «{line.name}» должны быть > 0")
        already = returned_qty.get(line.id, Decimal("0"))
        if _qty(already + qty) > _qty(line.quantity):
            raise WarehouseInvoiceError(f"Гашение по «{line.name}» превышает остаток")
        # Копим в снимке: дубли одной позиции в запросе иначе прошли бы лимит по отдельности
        # и создали бы фантомный приход денег сверх выданного товара.
        returned_qty[line.id] = _qty(already + qty)
        money_total += _money(qty * price)
        credited_total += _money(qty * _money(line.price))
        resolved.append((line, qty, _money(qty * price)))

    if bank_operation_id is not None:
        operation = await session.get(BankOperation, bank_operation_id)
        if operation is None:
            raise WarehouseInvoiceError("Банковская операция не найдена")
        if operation.direction != "in":
            raise WarehouseInvoiceError("Гашение нам — входящая операция, выбрана исходящая")
        # Занятость операции: одна выписочная строка не может закрыть два займа.
        await _assert_operation_free(session, operation, money_total)
        transaction = None
        if operation.cashflow_transaction_id is not None:
            transaction = await session.get(
                CashflowTransaction, operation.cashflow_transaction_id
            )
        if transaction is None:
            from app.services.banking.classifier import _wallet_for_operation

            wallet = await _wallet_for_operation(session, operation)
            if wallet is None:
                raise WarehouseInvoiceError("Кошелёк банковского счёта операции не найден")
            transaction = CashflowTransaction(
                wallet_id=wallet.id,
                direction="in",
                amount=money_total,
                operation_date=operation.operation_date,
                article_id=await _refund_article_id(session),
                counterparty_id=loan.counterparty_id,
                source_kind="bank_operation",
                source_id=operation.id,
                payment_purpose=operation.payment_purpose,
                quality_status="manual_override",
            )
            session.add(transaction)
            await session.flush()
            operation.cashflow_transaction_id = transaction.id
            operation.classification_status = "classified"
        else:
            transaction.counterparty_id = loan.counterparty_id
    else:
        wallet = await session.get(Wallet, wallet_id)
        if wallet is None or wallet.status != "active":
            raise WarehouseInvoiceError("Счёт не найден или неактивен")
        if wallet.type == "bank":
            # Приход на БАНКОВСКИЙ кошелёк придёт ещё и выпиской: источник 'barter_return' не
            # входит в PREBOOKABLE_SOURCE_KINDS, склейки не будет → ДДС задвоится. Банковские
            # деньги проводим только через канал «банк-операция» (привязка к строке выписки).
            raise WarehouseInvoiceError(
                "Для денег на расчётном счёте выберите канал «Банк-операция» — "
                "иначе приход задвоится, когда придёт выписка"
            )
        transaction = CashflowTransaction(
            wallet_id=wallet.id,
            direction="in",
            amount=money_total,
            operation_date=operation_date,
            article_id=await _refund_article_id(session),
            counterparty_id=loan.counterparty_id,
            source_kind="barter_return",
            source_id=loan.id,
            payment_purpose=f"Гашение бартерного займа {loan.number or ''} деньгами".strip(),
            quality_status="manual_override",
        )
        session.add(transaction)
        await session.flush()

    for line, qty, amount in resolved:
        session.add(
            BarterReturnLine(
                return_invoice_id=None,
                cashflow_transaction_id=transaction.id,
                loan_invoice_id=loan.id,
                loan_line_item_id=line.id,
                quantity=qty,
                amount=amount,
                created_by_user_id=actor_user_id,
            )
        )
    await session.flush()
    remaining = await sync_barter_loan_status(session, loan)
    await session.commit()
    return {
        "loan_id": str(loan.id),
        "money_received": float(money_total),
        "credited": float(credited_total),
        "remaining": float(remaining),
        "status": loan.barter_return_status,
        "cashflow_transaction_id": str(transaction.id),
    }


async def pay_payable_loan_with_money(
    session: AsyncSession,
    *,
    loan_id: uuid.UUID,
    operation_date: date,
    amount: Decimal,
    wallet_id: uuid.UUID | None = None,
    bank_operation_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Мы гасим ИХ заём деньгами — «как обычную поставку», по сумме накладной.

    Нал — обычная оплата накладной с кошелька (``pay_invoice_from_wallet``: проводка +
    аллокация); банк — привязка исходящей операции выписки (bank-аллокация с гардом бюджета
    платежа). Лимит — остаток по ЗАЧЁТНОЙ стоимости: товарные возвраты уже уменьшили долг,
    аллокационного остатка накладной для этого мало."""
    loan = await _load_loan(session, loan_id)
    if loan.direction != "payable":
        raise WarehouseInvoiceError("Это наш заём — его гасит контрагент (кг × текущая цена)")
    if (wallet_id is None) == (bank_operation_id is None):
        raise WarehouseInvoiceError("Выберите ровно один канал денег: кошелёк или банк-операцию")

    requested = _money(amount)
    remaining = _money(loan.amount) - await loan_settled_value(session, loan)
    if requested <= 0 or requested > remaining:
        raise WarehouseInvoiceError(
            f"Сумма {requested} вне остатка займа {max(remaining, Decimal('0.00'))}"
        )

    if wallet_id is not None:
        from app.services.counterparty_payments import (
            CounterpartyPaymentError,
            pay_invoice_from_wallet,
        )

        try:
            await pay_invoice_from_wallet(
                session,
                invoice_id=loan.id,
                wallet_id=wallet_id,
                amount=requested,
                operation_date=operation_date,
                comment=f"Оплата бартерного займа {loan.number or ''}".strip(),
                actor_user_id=actor_user_id,
            )
        except CounterpartyPaymentError as exc:
            raise WarehouseInvoiceError(str(exc)) from exc
    else:
        from app.services.counterparty_bank_match import (
            CounterpartyMatchError,
            confirm_invoice_match,
        )

        try:
            await confirm_invoice_match(
                session,
                invoice_id=loan.id,
                bank_operation_id=bank_operation_id,
                enrich=False,
                actor_user_id=actor_user_id,
                allow_barter_loan=True,
                # ЯВНАЯ сумма: остаток по аллокациям не знает товарных возвратов (они в
                # BarterReturnLine), без неё операция закрыла бы уже возвращённое вторично.
                amount=requested,
            )
        except CounterpartyMatchError as exc:
            raise WarehouseInvoiceError(str(exc)) from exc
        await _resync_rule1_prepayment(session, bank_operation_id)

    remaining = await sync_barter_loan_status(session, loan)
    await session.commit()
    return {
        "loan_id": str(loan.id),
        "paid": float(requested),
        "remaining": float(remaining),
        "status": loan.barter_return_status,
    }

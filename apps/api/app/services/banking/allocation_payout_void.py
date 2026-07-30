"""Отмена ОШИБОЧНОЙ выплаты резерва: с Сейфа (``safe_payout``) и из кассы
(``kassa_target_payout``).

Выплата резерва была необратима в обе стороны: журнал кассы разбирает только
``kassa_payout``/``salary_advance``/``supplier_prepayment``, а у ленты ДДС правки нет вовсе.
Ошиблись суммой, счётом или просто провели дважды — расход висел вечно, резерв числился
оплаченным, и вернуть его в «К выдаче» было нечем.

Отменяется именно ОПЕРАЦИЯ (одна проводка), а не резерв: у резерва может быть несколько
частичных оплат, и снимается ровно та, что указана. Резерв откатывается на её сумму и
возвращается в активные (``partially_paid`` → ``reserved``, если оплат больше не осталось).

Деньги — по кассовому канону, тому же, что у отмены выдачи внештатнику:

* выплата СЕГОДНЯ — проводка удаляется (в журнале не остаётся ни расхода, ни сторно);
* выплата ЗА ПРОШЛУЮ ДАТУ — исходный расход остаётся, к нему пишется сторно-приход.

**Чего этот контур НЕ делает.** Оплата резерва тянет за собой цепочку в чужих контурах:
гасит накладные поставщика и аренду, заводит дебиторку-предоплату, пишет выплату сотруднику,
списывает депозит и шлёт изъятие в iiko. Разматывать их отсюда — значит незаметно править
четыре чужих учёта, а изъятие в iiko вообще не отзывается. Поэтому такие выплаты не
отменяются молча и не отменяются наполовину: контур отвечает отказом, который прямо называет,
что именно держит операцию и в каком модуле это распроводится. Исключение — предоплата
поставщику: у неё есть собственный безопасный откат (``cancel_supplier_prepayment``), он и
вызывается, но только пока дебиторка не тронута ни рублём.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CashflowTransaction,
    EmployeePayout,
    InvoicePaymentAllocation,
    SafeAllocation,
    SupplierPrepayment,
    Wallet,
)
from app.services.banking.safe_allocations import (
    KASSA_TARGET_PAYOUT_SOURCE_KIND,
    SAFE_PAYOUT_SOURCE_KIND,
)

# Обе выплаты резерва — один механизм (``pay_allocation``), различаются только кошельком.
VOIDABLE_PAYOUT_SOURCE_KINDS = (SAFE_PAYOUT_SOURCE_KIND, KASSA_TARGET_PAYOUT_SOURCE_KIND)

_CENTS = Decimal("0.01")


class AllocationPayoutVoidError(Exception):
    """Отмена выплаты резерва невозможна (валидация, состояние, чужой контур)."""


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(_CENTS)


async def _guard_untouched_by_other_flows(
    session: AsyncSession, transaction: CashflowTransaction, allocation: SafeAllocation
) -> SupplierPrepayment | None:
    """Убедиться, что выплату не держит чужой учёт; вернуть предоплату, если она есть.

    Каждый отказ называет причину человеческим языком: администратор должен понять, где
    распроводить, а не гадать, почему кнопка не сработала.
    """
    if allocation.source_run_id is not None:
        raise AllocationPayoutVoidError(
            "Это резерв выплаты зарплаты — отменяйте выплату в окне ведомости, не здесь"
        )

    # Депозит: выдача списала депозит-счёт сотрудника и ушла изъятием в iiko — обратного хода нет.
    from app.services.deposit_bank_draft import allocation_deposit_draft

    if await allocation_deposit_draft(session, allocation.id) is not None:
        raise AllocationPayoutVoidError(
            "Выплата депозита сотруднику: депозит уже списан, а изъятие ушло в iiko — "
            "отмена только через модуль «Депозиты»"
        )

    # Банк-выдача аванса/займа: полная оплата резерва формирует долг сотрудника и тоже
    # зеркалится изъятием в iiko. Снимается своей отменой аванса, а не отсюда.
    from app.services.banking.safe_allocations import allocation_advance_draft_id

    if await allocation_advance_draft_id(session, allocation.id) is not None:
        raise AllocationPayoutVoidError(
            "Выплата аванса или займа сотруднику: долг уже сформирован — "
            "отменяйте её на странице «Авансы и займы»"
        )

    payout_id = await session.scalar(
        select(EmployeePayout.id).where(
            EmployeePayout.cashflow_transaction_id == transaction.id
        )
    )
    if payout_id is not None:
        raise AllocationPayoutVoidError(
            "Выплата учтена как выплата сотруднику и уже влияет на расчёт зарплаты — "
            "снимайте её в зарплатном контуре"
        )

    # Один и тот же признак закрывает и накладные закупа, и арендное обязательство:
    # оба гасятся через InvoicePaymentAllocation с этой проводкой.
    settled_invoice_id = await session.scalar(
        select(InvoicePaymentAllocation.invoice_id).where(
            InvoicePaymentAllocation.cashflow_transaction_id == transaction.id
        )
    )
    if settled_invoice_id is not None:
        raise AllocationPayoutVoidError(
            "Этой выплатой погашены документы поставщика (накладные или аренда) — "
            "сначала снимите оплату в карточке документа"
        )

    return await session.scalar(
        select(SupplierPrepayment).where(
            SupplierPrepayment.cashflow_transaction_id == transaction.id
        )
    )


def _rollback_allocation(allocation: SafeAllocation, amount: Decimal) -> None:
    """Вернуть резерву неоплаченный остаток: оплата снята, резерв снова ждёт денег."""
    paid = _money(allocation.amount_paid) - amount
    allocation.amount_paid = paid if paid > 0 else Decimal("0.00")
    allocation.status = "partially_paid" if allocation.amount_paid > 0 else "reserved"


async def void_allocation_payout(
    session: AsyncSession,
    *,
    transaction_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Отменить одну ошибочную выплату резерва (с Сейфа или из кассы).

    Возвращает сводку: режим (``deleted``/``reversed``), сумма, счёт, id сторно-проводки.
    """
    from app.services.kassa.payouts import kassa_today

    transaction = await session.get(CashflowTransaction, transaction_id)
    if transaction is None:
        raise AllocationPayoutVoidError("Операция не найдена")
    if transaction.source_kind not in VOIDABLE_PAYOUT_SOURCE_KINDS:
        raise AllocationPayoutVoidError(
            "Эта операция — не выплата резерва, из этого окна она не отменяется"
        )
    if transaction.direction != "out":
        raise AllocationPayoutVoidError(
            "Это сторно уже отменённой выплаты — отменять его повторно нечего"
        )
    if transaction.source_id is None:
        raise AllocationPayoutVoidError("У операции нет резерва-источника — отмена невозможна")

    allocation = await session.get(SafeAllocation, transaction.source_id, with_for_update=True)
    if allocation is None:
        raise AllocationPayoutVoidError("Резерв по этой выплате не найден")

    prepayment = await _guard_untouched_by_other_flows(session, transaction, allocation)

    amount = _money(transaction.amount)
    operation_date = today or kassa_today()
    wallet_name = await session.scalar(
        select(Wallet.name).where(Wallet.id == transaction.wallet_id)
    )

    if prepayment is not None:
        # Предоплата поставщику. Её собственный откат снимает и дебиторку, и проводку разом,
        # поэтому «сторно задним числом» здесь неприменимо — за прошлую дату отказываем и
        # отправляем в контур предоплат, где такие правки и место.
        if transaction.operation_date != operation_date:
            raise AllocationPayoutVoidError(
                "Выплата завела предоплату поставщику: отменить её можно только в день "
                "выплаты, позже — через карточку предоплаты"
            )
        from app.services.counterparty_payments import CounterpartyPaymentError
        from app.services.supplier_prepayments import cancel_supplier_prepayment

        _rollback_allocation(allocation, amount)
        try:
            # Сам удалит и предоплату, и проводку, и закоммитит.
            await cancel_supplier_prepayment(session, prepayment.id)
        except CounterpartyPaymentError as exc:
            raise AllocationPayoutVoidError(str(exc)) from exc
        return {
            "mode": "deleted",
            "amount": float(amount),
            "wallet_name": wallet_name,
            "prepayment_cancelled": True,
            "reversal_transaction_id": None,
        }

    reversal_id: uuid.UUID | None = None
    if transaction.operation_date == operation_date:
        await session.delete(transaction)
        mode = "deleted"
    else:
        reversal = CashflowTransaction(
            wallet_id=transaction.wallet_id,
            direction="in",
            amount=amount,
            operation_date=operation_date,
            article_id=transaction.article_id,
            counterparty_id=transaction.counterparty_id,
            location_id=transaction.location_id,
            lease_id=transaction.lease_id,
            source_kind=transaction.source_kind,
            source_id=allocation.id,
            payment_purpose=f"Отмена выплаты: {allocation.purpose or 'резерв'}",
            quality_status="final",
            created_by_user_id=actor_user_id,
        )
        session.add(reversal)
        await session.flush()
        reversal_id = reversal.id
        mode = "reversed"

    _rollback_allocation(allocation, amount)
    await session.commit()

    return {
        "mode": mode,
        "amount": float(amount),
        "wallet_name": wallet_name,
        "prepayment_cancelled": False,
        "reversal_transaction_id": reversal_id,
    }

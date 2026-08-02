"""Остатки ДЗ/КЗ с контрагентами НА ДАТУ — то, без чего баланс не собрать.

ЧЕМ ОТЛИЧАЕТСЯ ОТ ПЛИТКИ «ОСТАТКИ». Та отвечает на вопрос «сколько нам должны и сколько должны
мы ПРЯМО СЕЙЧАС»: берёт текущие статусы и текущие суммы гашений. Для баланса этого мало —
баланс собирается на конец месяца, и вопрос звучит иначе: «сколько было должно на 31 июля».
Ответить на него текущими статусами нельзя: документ, оплаченный 5 августа, сегодня закрыт,
а 31 июля был живой кредиторкой.

КАК СЧИТАЕМ. Обязательство существует на дату, если документ к ней уже вступил в силу
(``invoice_date <= as_of`` — правило 4 канона), и гасится теми аллокациями, чьё СОБЫТИЕ
произошло не позже даты. Дебиторка — зеркально: предоплата существует с даты своего денежного
факта и уменьшается гашениями до даты.

ДАТА СОБЫТИЯ У ГАШЕНИЯ. У аллокации есть только ``created_at`` — когда строку записали в
систему. Для денежных гашений это не то же самое, что дата платежа: выписку разбирают через
день-два, а иногда через неделю. Поэтому дату берём из самого денежного факта:

* ``source_kind='cash'``/банк → дата проводки или банковской операции;
* ``source_kind='prepayment'`` → дата документа, который гасят: обязательство и его закрытие
  предоплатой возникают одним событием — приходом документа;
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

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    BankOperation,
    CashflowTransaction,
    Counterparty,
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierPrepayment,
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


def _allocation_event_date():
    """Дата хозяйственного события аллокации — COALESCE по трём источникам.

    Порядок важен: у денежного гашения дата есть и у проводки, и у банковской операции, и
    брать надо ту, что ближе к факту. ``created_at`` — последний рубеж, а не первый выбор.
    """
    return func.coalesce(
        CashflowTransaction.operation_date,
        BankOperation.operation_date,
        SupplierInvoice.invoice_date,
        func.date(InvoicePaymentAllocation.created_at),
    )


async def build_balance_as_of(
    session: AsyncSession, *, as_of: date
) -> BalanceSheetAsOf:
    """Остатки расчётов с контрагентами на конец указанной даты (включительно)."""
    event_date = _allocation_event_date()
    settled_by_invoice = (
        select(
            InvoicePaymentAllocation.invoice_id.label("invoice_id"),
            func.sum(InvoicePaymentAllocation.amount).label("settled"),
            func.sum(
                case(
                    (
                        CashflowTransaction.operation_date.is_(None)
                        & BankOperation.operation_date.is_(None)
                        & SupplierInvoice.invoice_date.is_(None),
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
        .outerjoin(
            BankOperation, BankOperation.id == InvoicePaymentAllocation.bank_operation_id
        )
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
            .outerjoin(
                settled_by_invoice, settled_by_invoice.c.invoice_id == SupplierInvoice.id
            )
            .where(
                *_DOC_CONDITIONS,
                # Правило 4 канона: документ становится обязательством своей датой. Документ
                # без даты считаем действующим с момента записи — других ориентиров нет.
                func.coalesce(SupplierInvoice.invoice_date, func.date(SupplierInvoice.created_at))
                <= as_of,
            )
            .group_by(SupplierInvoice.counterparty_id)
        )
    ).all()

    # Дебиторка: предоплата живёт с даты своего денежного факта и гасится аллокациями до даты.
    settled_by_prepayment = (
        select(
            InvoicePaymentAllocation.prepayment_id.label("prepayment_id"),
            func.sum(InvoicePaymentAllocation.amount).label("settled"),
        )
        .outerjoin(SupplierInvoice, SupplierInvoice.id == InvoicePaymentAllocation.invoice_id)
        .where(
            InvoicePaymentAllocation.prepayment_id.is_not(None),
            func.coalesce(
                SupplierInvoice.invoice_date,
                func.date(InvoicePaymentAllocation.created_at),
            )
            <= as_of,
        )
        .group_by(InvoicePaymentAllocation.prepayment_id)
        .subquery()
    )
    prepayment_date = func.coalesce(
        CashflowTransaction.operation_date, func.date(SupplierPrepayment.created_at)
    )
    receivable_rows = (
        await session.execute(
            select(
                SupplierPrepayment.counterparty_id,
                func.sum(
                    func.greatest(
                        SupplierPrepayment.amount
                        - func.coalesce(settled_by_prepayment.c.settled, 0),
                        0,
                    )
                ),
            )
            .outerjoin(
                CashflowTransaction,
                CashflowTransaction.id == SupplierPrepayment.cashflow_transaction_id,
            )
            .outerjoin(
                settled_by_prepayment,
                settled_by_prepayment.c.prepayment_id == SupplierPrepayment.id,
            )
            .where(SupplierPrepayment.status != "refunded", prepayment_date <= as_of)
            .group_by(SupplierPrepayment.counterparty_id)
        )
    ).all()

    payable_by_cp = {row[0]: money(row[1]) for row in payable_rows}
    approximate = money(sum((row[2] or Decimal("0") for row in payable_rows), Decimal("0")))
    receivable_by_cp = {row[0]: money(row[1]) for row in receivable_rows}
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

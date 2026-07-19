"""Учёт ДЗ/КЗ поставщиков и журнал признания расходов по периодам услуг."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, ensure_permission, get_current_actor, require_permission
from app.db.session import get_session
from app.models import (
    BarterReturnLine,
    CashflowTransaction,
    Counterparty,
    CounterpartyPaymentDraft,
    DdsArticle,
    Employee,
    InvoiceLineItem,
    InvoicePaymentAllocation,
    PayrollLine,
    PayrollPayment,
    PayrollPeriod,
    PayrollRun,
    SalaryAdvance,
    SupplierExpenseAccrual,
    SupplierInvoice,
    SupplierPrepayment,
    Wallet,
)
from app.services import supplier_service_periods as periods
from app.services.payroll_advance_availability import available_to_advance

OPEN_PREPAYMENT_STATUSES = ("open", "partially_settled")
UNPAID_INVOICE_STATUSES = ("unpaid", "partially_paid")

router = APIRouter()
READ = (Depends(require_permission("accounting.suppliers.read")),)
EDIT = (Depends(require_permission("accounting.suppliers.edit")),)


class SupplierAccountingItem(BaseModel):
    id: uuid.UUID
    source_kind: Literal["service_period", "legacy_prepayment"]
    counterparty_id: uuid.UUID
    counterparty_name: str
    article_id: uuid.UUID | None = None
    article_name: str | None = None
    invoice_id: uuid.UUID | None = None
    invoice_number: str | None = None
    amount: float
    paid_amount: float
    balance_amount: float
    balance_type: Literal["receivable", "payable", "scheduled", "closed", "needs_review"]
    service_period_start: date | None = None
    service_period_end: date | None = None
    period_status: str
    recognition_month: date | None = None
    recognized: bool


class SupplierAccountingList(BaseModel):
    items: list[SupplierAccountingItem]
    receivable_total: float
    payable_total: float
    scheduled_total: float
    needs_review_total: float


class ServicePeriodUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_period_start: date
    service_period_end: date
    reason: str | None = Field(default=None, max_length=500)


def _float(value: Decimal | int | float | None) -> float:
    return float(periods.money(value))


@router.get("", response_model=SupplierAccountingList, dependencies=READ)
async def list_supplier_accounting(
    session: Annotated[AsyncSession, Depends(get_session)],
    view: Literal["open", "all", "needs_review", "recognized"] = Query(default="open"),
) -> SupplierAccountingList:
    allocated = (
        select(
            InvoicePaymentAllocation.invoice_id.label("invoice_id"),
            func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0).label("paid"),
        )
        .group_by(InvoicePaymentAllocation.invoice_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                SupplierExpenseAccrual,
                Counterparty.name,
                SupplierInvoice.number,
                SupplierInvoice.payment_status,
                func.coalesce(allocated.c.paid, 0),
                CounterpartyPaymentDraft.status,
                DdsArticle.name,
            )
            .join(Counterparty, Counterparty.id == SupplierExpenseAccrual.counterparty_id)
            .outerjoin(SupplierInvoice, SupplierInvoice.id == SupplierExpenseAccrual.invoice_id)
            .outerjoin(allocated, allocated.c.invoice_id == SupplierExpenseAccrual.invoice_id)
            .outerjoin(
                CounterpartyPaymentDraft,
                CounterpartyPaymentDraft.id == SupplierExpenseAccrual.payment_draft_id,
            )
            .outerjoin(DdsArticle, DdsArticle.id == SupplierExpenseAccrual.article_id)
            .where(SupplierExpenseAccrual.status != "cancelled")
            .order_by(
                SupplierExpenseAccrual.service_period_end.desc(),
                SupplierExpenseAccrual.created_at.desc(),
            )
        )
    ).all()

    items: list[SupplierAccountingItem] = []
    for (
        accrual,
        cp_name,
        number,
        _invoice_status,
        allocated_amount,
        draft_status,
        article_name,
    ) in rows:
        if accrual.invoice_id is not None:
            paid = min(periods.money(allocated_amount), periods.money(accrual.amount))
        else:
            paid = periods.money(accrual.amount) if draft_status == "paid" else Decimal("0")
        total = periods.money(accrual.amount)
        if accrual.status == "recognized":
            balance = max(total - paid, Decimal("0"))
            balance_type = "payable" if balance > 0 else "closed"
        elif paid > 0:
            balance = paid
            balance_type = "receivable"
        else:
            balance = total
            balance_type = "scheduled"
        item = SupplierAccountingItem(
            id=accrual.id,
            source_kind="service_period",
            counterparty_id=accrual.counterparty_id,
            counterparty_name=cp_name,
            article_id=accrual.article_id,
            article_name=article_name,
            invoice_id=accrual.invoice_id,
            invoice_number=number,
            amount=_float(total),
            paid_amount=_float(paid),
            balance_amount=_float(balance),
            balance_type=balance_type,
            service_period_start=accrual.service_period_start,
            service_period_end=accrual.service_period_end,
            period_status="ready",
            recognition_month=accrual.recognition_month,
            recognized=accrual.status == "recognized",
        )
        items.append(item)

    # Ранее заведённые и ещё не закрытые авансы остаются видимыми. Без периода они не
    # признаются автоматически и формируют очередь ручного распределения.
    prepayment_rows = (
        await session.execute(
            select(SupplierPrepayment, Counterparty.name, DdsArticle.name)
            .join(Counterparty, Counterparty.id == SupplierPrepayment.counterparty_id)
            .outerjoin(DdsArticle, DdsArticle.id == SupplierPrepayment.article_id)
            .where(SupplierPrepayment.status.in_(("open", "partially_settled")))
            # ДЗ по оплаченному счёту (kind='prepaid_bill', единый чокпоинт канона) гасится
            # закрывающим УПД (правило 2), а НЕ ручным распределением по периодам — из очереди
            # «Признание расходов» её исключаем (в дебиторку /balances она входит отдельно).
            .where(SupplierPrepayment.kind != "prepaid_bill")
            .order_by(SupplierPrepayment.created_at.desc())
        )
    ).all()
    for prepayment, cp_name, article_name in prepayment_rows:
        balance = max(
            periods.money(prepayment.amount) - periods.money(prepayment.amount_settled),
            Decimal("0"),
        )
        needs_review = (
            prepayment.service_period_status != "ready"
            or prepayment.service_period_start is None
            or prepayment.service_period_end is None
        )
        items.append(
            SupplierAccountingItem(
                id=prepayment.id,
                source_kind="legacy_prepayment",
                counterparty_id=prepayment.counterparty_id,
                counterparty_name=cp_name,
                article_id=prepayment.article_id,
                article_name=article_name,
                amount=_float(prepayment.amount),
                paid_amount=_float(prepayment.amount),
                balance_amount=_float(balance),
                balance_type="needs_review" if needs_review else "receivable",
                service_period_start=prepayment.service_period_start,
                service_period_end=prepayment.service_period_end,
                period_status=prepayment.service_period_status,
                recognized=False,
            )
        )

    if view == "open":
        items = [item for item in items if item.balance_type != "closed"]
    elif view == "needs_review":
        items = [item for item in items if item.balance_type == "needs_review"]
    elif view == "recognized":
        items = [item for item in items if item.recognized]

    return SupplierAccountingList(
        items=items,
        receivable_total=sum(
            item.balance_amount for item in items if item.balance_type == "receivable"
        ),
        payable_total=sum(item.balance_amount for item in items if item.balance_type == "payable"),
        scheduled_total=sum(
            item.balance_amount for item in items if item.balance_type == "scheduled"
        ),
        needs_review_total=sum(
            item.balance_amount for item in items if item.balance_type == "needs_review"
        ),
    )


@router.patch(
    "/service-periods/{accrual_id}",
    response_model=SupplierAccountingItem,
    dependencies=EDIT,
)
async def patch_service_period(
    accrual_id: uuid.UUID,
    payload: ServicePeriodUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> SupplierAccountingItem:
    accrual = await session.get(SupplierExpenseAccrual, accrual_id)
    if accrual is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Начисление не найдено")
    if accrual.status == "recognized":
        ensure_permission(actor, "accounting.service_periods.correct_recognized")
        if not (payload.reason or "").strip():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Укажите причину корректировки уже признанного расхода",
            )
    try:
        await periods.change_accrual_period(
            session,
            accrual=accrual,
            start=payload.service_period_start,
            end=payload.service_period_end,
            actor_user_id=actor.user_id,
            reason=payload.reason,
        )
    except periods.ServicePeriodError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    cp_name = await session.scalar(
        select(Counterparty.name).where(Counterparty.id == accrual.counterparty_id)
    )
    return SupplierAccountingItem(
        id=accrual.id,
        source_kind="service_period",
        counterparty_id=accrual.counterparty_id,
        counterparty_name=cp_name or "—",
        article_id=accrual.article_id,
        invoice_id=accrual.invoice_id,
        amount=_float(accrual.amount),
        paid_amount=0,
        balance_amount=_float(accrual.amount),
        balance_type="scheduled" if accrual.status == "scheduled" else "payable",
        service_period_start=accrual.service_period_start,
        service_period_end=accrual.service_period_end,
        period_status="ready",
        recognition_month=accrual.recognition_month,
        recognized=accrual.status == "recognized",
    )


# --- Дашборд взаиморасчётов: остатки по контрагентам, реестр платежей, реестр УПД ---


class CounterpartyBalance(BaseModel):
    counterparty_id: uuid.UUID
    name: str
    inn: str | None = None
    receivable: float
    payable: float
    net: float
    open_prepayments: int
    unpaid_invoices: int
    last_activity: date | None = None


class CounterpartyBalanceList(BaseModel):
    items: list[CounterpartyBalance]
    receivable_total: float
    payable_total: float


class SettledInvoiceRef(BaseModel):
    invoice_id: uuid.UUID
    number: str | None = None
    invoice_date: date | None = None
    amount: float


class PaymentPrepaymentInfo(BaseModel):
    id: uuid.UUID
    kind: str
    status: str
    amount: float
    amount_settled: float
    settled_invoices: list[SettledInvoiceRef]


class PaymentRegisterRow(BaseModel):
    id: uuid.UUID
    row_kind: Literal["transaction", "opening_prepayment"]
    operation_date: date
    amount: float
    counterparty_id: uuid.UUID
    counterparty_name: str
    wallet_name: str | None = None
    article_name: str | None = None
    purpose: str | None = None
    settled_invoices: list[SettledInvoiceRef]
    prepayment: PaymentPrepaymentInfo | None = None
    unassigned_amount: float


class PaymentRegisterList(BaseModel):
    items: list[PaymentRegisterRow]
    total_amount: float


class DocumentAllocationRef(BaseModel):
    source_kind: str
    amount: float
    operation_date: date | None = None
    prepayment_kind: str | None = None


class DocumentRegisterRow(BaseModel):
    invoice_id: uuid.UUID
    number: str | None = None
    invoice_date: date | None = None
    source: str
    doc_kind: str
    # 'active' — документ в силе (в КЗ); 'pending' — будущий УПД, ждёт своей даты (правило 4).
    activation_status: str
    counterparty_id: uuid.UUID
    counterparty_name: str
    amount: float
    payment_status: str
    remainder: float
    service_period_start: date | None = None
    service_period_end: date | None = None
    allocations: list[DocumentAllocationRef]


class DocumentRegisterList(BaseModel):
    items: list[DocumentRegisterRow]
    total_amount: float
    unpaid_total: float


def _clamp_money(value: Decimal) -> Decimal:
    return max(periods.money(value), Decimal("0"))


@router.get("/balances", response_model=CounterpartyBalanceList, dependencies=READ)
async def list_counterparty_balances(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CounterpartyBalanceList:
    """Актуальные остатки взаиморасчётов по каждому контрагенту.

    Дебиторка = открытые предоплаты (нам должны закрыть документами или вернуть).
    Кредиторка = неоплаченный остаток накладных direction='payable' (мы должны).
    Бартерные receivable-накладные сюда не входят — у бартера свой нетто-контур.
    """
    prepay_rows = (
        await session.execute(
            select(
                SupplierPrepayment.counterparty_id,
                func.sum(SupplierPrepayment.amount - SupplierPrepayment.amount_settled),
                func.count(SupplierPrepayment.id),
                func.max(func.date(SupplierPrepayment.created_at)),
            )
            .where(SupplierPrepayment.status.in_(OPEN_PREPAYMENT_STATUSES))
            .group_by(SupplierPrepayment.counterparty_id)
        )
    ).all()

    allocated = (
        select(
            InvoicePaymentAllocation.invoice_id.label("invoice_id"),
            func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0).label("paid"),
        )
        .group_by(InvoicePaymentAllocation.invoice_id)
        .subquery()
    )
    remainder = SupplierInvoice.amount - func.coalesce(allocated.c.paid, 0)
    invoice_rows = (
        await session.execute(
            select(
                SupplierInvoice.counterparty_id,
                func.sum(func.greatest(remainder, 0)),
                func.count(SupplierInvoice.id),
                func.max(SupplierInvoice.invoice_date),
            )
            .outerjoin(allocated, allocated.c.invoice_id == SupplierInvoice.id)
            .where(
                SupplierInvoice.payment_status.in_(UNPAID_INVOICE_STATUSES),
                SupplierInvoice.direction == "payable",
                # Канон ДЗ/КЗ: кредиторка — это АКТИВНЫЕ закрывающие документы. Счета (bill) —
                # не долг (очередь оплат), будущие УПД (activation='pending') ещё не в силе.
                SupplierInvoice.doc_kind == "closing",
                SupplierInvoice.activation_status == "active",
            )
            .group_by(SupplierInvoice.counterparty_id)
        )
    ).all()

    # Контрагенты с закрытыми расчётами (0/0) остаются в списке: владелец видит ВСЕХ,
    # с кем есть документооборот. Счета (bill) в баланс не входят и в этот список не тянут —
    # они живут в очереди оплат; учитываем только закрывающие документы (в т.ч. будущие УПД).
    activity_rows = (
        await session.execute(
            select(
                SupplierInvoice.counterparty_id,
                func.max(SupplierInvoice.invoice_date),
            )
            .where(
                SupplierInvoice.payment_status != "void",
                SupplierInvoice.direction == "payable",
                SupplierInvoice.doc_kind == "closing",
            )
            .group_by(SupplierInvoice.counterparty_id)
        )
    ).all()
    prepay_activity_rows = (
        await session.execute(
            select(
                SupplierPrepayment.counterparty_id,
                func.max(func.date(SupplierPrepayment.created_at)),
            ).group_by(SupplierPrepayment.counterparty_id)
        )
    ).all()

    # Бартерные займы — товарные долги в ОБЩИХ плитках (решение владельца 18.07). Их заём нам
    # (payable) уже входит в кредиторку инвойс-строками, но остаток там аллокационный — вычитаем
    # ЗАЧЁТНУЮ стоимость возвратов (qty × исходная цена займа; свободные суммы — как есть),
    # иначе частично возвращённый заём висит полной суммой. Наша выдача (receivable-заём) —
    # дебиторка остатком. Денежные оплаты займов сидят в аллокациях и учтены самой плиткой.
    return_credit = (
        select(
            BarterReturnLine.loan_invoice_id.label("loan_id"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            BarterReturnLine.quantity.isnot(None)
                            & BarterReturnLine.loan_line_item_id.isnot(None),
                            BarterReturnLine.quantity * InvoiceLineItem.price,
                        ),
                        else_=BarterReturnLine.amount,
                    )
                ),
                0,
            ).label("credited"),
        )
        .outerjoin(InvoiceLineItem, InvoiceLineItem.id == BarterReturnLine.loan_line_item_id)
        .group_by(BarterReturnLine.loan_invoice_id)
        .subquery()
    )
    barter_payable_credit_rows = (
        await session.execute(
            select(SupplierInvoice.counterparty_id, func.sum(return_credit.c.credited))
            .join(return_credit, return_credit.c.loan_id == SupplierInvoice.id)
            .where(
                SupplierInvoice.direction == "payable",
                SupplierInvoice.barter_role == "loan",
                SupplierInvoice.payment_status.in_(UNPAID_INVOICE_STATUSES),
                SupplierInvoice.doc_kind == "closing",
                SupplierInvoice.activation_status == "active",
            )
            .group_by(SupplierInvoice.counterparty_id)
        )
    ).all()
    barter_receivable_rows = (
        await session.execute(
            select(
                SupplierInvoice.counterparty_id,
                func.sum(
                    func.greatest(
                        SupplierInvoice.amount - func.coalesce(return_credit.c.credited, 0), 0
                    )
                ),
                func.max(SupplierInvoice.invoice_date),
            )
            .outerjoin(return_credit, return_credit.c.loan_id == SupplierInvoice.id)
            .where(
                SupplierInvoice.direction == "receivable",
                SupplierInvoice.barter_role == "loan",
                SupplierInvoice.payment_status != "void",
                SupplierInvoice.barter_return_status != "returned",
            )
            .group_by(SupplierInvoice.counterparty_id)
        )
    ).all()

    receivable_by_cp = {row[0]: (periods.money(row[1]), row[2], row[3]) for row in prepay_rows}
    payable_by_cp = {row[0]: (periods.money(row[1]), row[2], row[3]) for row in invoice_rows}
    for cp_id, credited in barter_payable_credit_rows:
        if cp_id in payable_by_cp:
            total, cnt, last = payable_by_cp[cp_id]
            payable_by_cp[cp_id] = (max(total - periods.money(credited), Decimal("0")), cnt, last)
    for cp_id, loan_remaining, loan_last in barter_receivable_rows:
        total, cnt, prev_last = receivable_by_cp.get(cp_id, (Decimal("0"), 0, None))
        best_last = max(filter(None, (prev_last, loan_last)), default=None)
        receivable_by_cp[cp_id] = (total + periods.money(loan_remaining), cnt, best_last)
    activity_by_cp: dict[uuid.UUID, date] = {}
    for cp_id, last_date in list(activity_rows) + list(prepay_activity_rows):
        if last_date is None:
            continue
        current = activity_by_cp.get(cp_id)
        activity_by_cp[cp_id] = max(current, last_date) if current else last_date

    cp_ids = set(receivable_by_cp) | set(payable_by_cp) | set(activity_by_cp)
    if not cp_ids:
        return CounterpartyBalanceList(items=[], receivable_total=0, payable_total=0)

    counterparties = (
        await session.execute(
            select(Counterparty.id, Counterparty.name, Counterparty.inn).where(
                Counterparty.id.in_(cp_ids)
            )
        )
    ).all()

    items: list[CounterpartyBalance] = []
    for cp_id, name, inn in counterparties:
        receivable, prepay_count, prepay_last = receivable_by_cp.get(cp_id, (Decimal("0"), 0, None))
        payable, invoice_count, invoice_last = payable_by_cp.get(cp_id, (Decimal("0"), 0, None))
        last_activity = max(
            filter(None, (prepay_last, invoice_last, activity_by_cp.get(cp_id))), default=None
        )
        items.append(
            CounterpartyBalance(
                counterparty_id=cp_id,
                name=name,
                inn=inn,
                receivable=_float(receivable),
                payable=_float(payable),
                net=_float(receivable - payable),
                open_prepayments=prepay_count,
                unpaid_invoices=invoice_count,
                last_activity=last_activity,
            )
        )
    items.sort(
        key=lambda item: (max(item.receivable, item.payable), item.last_activity or date.min),
        reverse=True,
    )
    return CounterpartyBalanceList(
        items=items,
        receivable_total=sum(item.receivable for item in items),
        payable_total=sum(item.payable for item in items),
    )


async def _invoice_refs(
    session: AsyncSession, allocations: list[InvoicePaymentAllocation]
) -> dict[uuid.UUID, SettledInvoiceRef]:
    """Карточки УПД для набора аллокаций: ключ — invoice_id, сумма — по этой аллокации."""
    invoice_ids = {alloc.invoice_id for alloc in allocations}
    if not invoice_ids:
        return {}
    rows = (
        await session.execute(
            select(SupplierInvoice.id, SupplierInvoice.number, SupplierInvoice.invoice_date).where(
                SupplierInvoice.id.in_(invoice_ids)
            )
        )
    ).all()
    meta = {row[0]: (row[1], row[2]) for row in rows}
    refs: dict[uuid.UUID, SettledInvoiceRef] = {}
    for alloc in allocations:
        number, invoice_date = meta.get(alloc.invoice_id, (None, None))
        existing = refs.get(alloc.invoice_id)
        amount = periods.money(alloc.amount) + (
            Decimal(str(existing.amount)) if existing else Decimal("0")
        )
        refs[alloc.invoice_id] = SettledInvoiceRef(
            invoice_id=alloc.invoice_id,
            number=number,
            invoice_date=invoice_date,
            amount=_float(amount),
        )
    return refs


@router.get("/payments", response_model=PaymentRegisterList, dependencies=READ)
async def list_payment_register(
    session: Annotated[AsyncSession, Depends(get_session)],
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    counterparty_id: Annotated[uuid.UUID | None, Query()] = None,
) -> PaymentRegisterList:
    """Реестр платежей поставщикам: деньги ушли → чем это гасится.

    Строка — исходящая ДДС-проводка с контрагентом (банк/касса/карта) либо
    входящий остаток-предоплата без движения денег (opening). К строке пришиты:
    прямые гашения накладных (аллокации cash по transaction_id, bank через
    банк-операцию источника) и предоплата, созданная этим платежом, с её УПД.
    """
    tx_filters = [
        CashflowTransaction.direction == "out",
        CashflowTransaction.counterparty_id.is_not(None),
        CashflowTransaction.quality_status != "excluded",
    ]
    if date_from is not None:
        tx_filters.append(CashflowTransaction.operation_date >= date_from)
    if date_to is not None:
        tx_filters.append(CashflowTransaction.operation_date <= date_to)
    if counterparty_id is not None:
        tx_filters.append(CashflowTransaction.counterparty_id == counterparty_id)

    tx_rows = (
        await session.execute(
            select(CashflowTransaction, Wallet.name, DdsArticle.name, Counterparty.name)
            .join(Wallet, Wallet.id == CashflowTransaction.wallet_id)
            .outerjoin(DdsArticle, DdsArticle.id == CashflowTransaction.article_id)
            .join(Counterparty, Counterparty.id == CashflowTransaction.counterparty_id)
            .where(*tx_filters)
            .order_by(
                CashflowTransaction.operation_date.desc(), CashflowTransaction.created_at.desc()
            )
            .limit(1000)
        )
    ).all()

    tx_ids = [row[0].id for row in tx_rows]
    bank_op_to_tx = {
        row[0].source_id: row[0].id
        for row in tx_rows
        if row[0].source_kind == "bank_operation" and row[0].source_id is not None
    }

    direct_allocs: list[InvoicePaymentAllocation] = []
    if tx_ids or bank_op_to_tx:
        alloc_filters = []
        if tx_ids:
            alloc_filters.append(InvoicePaymentAllocation.cashflow_transaction_id.in_(tx_ids))
        if bank_op_to_tx:
            alloc_filters.append(
                InvoicePaymentAllocation.bank_operation_id.in_(bank_op_to_tx.keys())
            )
        direct_allocs = list(
            (
                await session.scalars(select(InvoicePaymentAllocation).where(or_(*alloc_filters)))
            ).all()
        )

    prepayments = (
        (
            await session.scalars(
                select(SupplierPrepayment).where(
                    SupplierPrepayment.cashflow_transaction_id.in_(tx_ids)
                )
            )
        ).all()
        if tx_ids
        else []
    )
    prepayment_by_tx = {sp.cashflow_transaction_id: sp for sp in prepayments}

    # ДЗ по оплаченному счёту (kind='prepaid_bill') несёт cashflow_transaction_id=None ПО ЗАМЫСЛУ
    # (денег не двигает — факт оплаты уже несёт аллокация счёта), поэтому без фильтра она попадала
    # в реестр строкой «начальный остаток» ВТОРЫМ разом поверх самой проводки платежа: один платёж
    # по счёту давал две строки и задвоенный итог периода. Настоящие опенинги (POST
    # /prepayments/opening) остаются — у них другой kind.
    opening_filters = [
        SupplierPrepayment.cashflow_transaction_id.is_(None),
        SupplierPrepayment.kind != "prepaid_bill",
    ]
    if counterparty_id is not None:
        opening_filters.append(SupplierPrepayment.counterparty_id == counterparty_id)
    if date_from is not None:
        opening_filters.append(func.date(SupplierPrepayment.created_at) >= date_from)
    if date_to is not None:
        opening_filters.append(func.date(SupplierPrepayment.created_at) <= date_to)
    opening_rows = (
        await session.execute(
            select(SupplierPrepayment, Counterparty.name, DdsArticle.name)
            .join(Counterparty, Counterparty.id == SupplierPrepayment.counterparty_id)
            .outerjoin(DdsArticle, DdsArticle.id == SupplierPrepayment.article_id)
            .where(*opening_filters)
            .order_by(SupplierPrepayment.created_at.desc())
            .limit(500)
        )
    ).all()

    prepay_allocs: list[InvoicePaymentAllocation] = []
    prepay_ids = [sp.id for sp in prepayments] + [row[0].id for row in opening_rows]
    if prepay_ids:
        prepay_allocs = list(
            (
                await session.scalars(
                    select(InvoicePaymentAllocation).where(
                        InvoicePaymentAllocation.prepayment_id.in_(prepay_ids)
                    )
                )
            ).all()
        )

    all_invoice_refs = await _invoice_refs(session, direct_allocs + prepay_allocs)

    def refs_for(allocs: list[InvoicePaymentAllocation]) -> list[SettledInvoiceRef]:
        merged: dict[uuid.UUID, Decimal] = {}
        for alloc in allocs:
            merged[alloc.invoice_id] = merged.get(alloc.invoice_id, Decimal("0")) + periods.money(
                alloc.amount
            )
        result = []
        for invoice_id, amount in merged.items():
            base = all_invoice_refs[invoice_id]
            result.append(base.model_copy(update={"amount": _float(amount)}))
        result.sort(key=lambda ref: ref.invoice_date or date.min, reverse=True)
        return result

    prepay_allocs_by_id: dict[uuid.UUID, list[InvoicePaymentAllocation]] = {}
    for alloc in prepay_allocs:
        if alloc.prepayment_id is not None:
            prepay_allocs_by_id.setdefault(alloc.prepayment_id, []).append(alloc)

    def prepayment_info(sp: SupplierPrepayment) -> PaymentPrepaymentInfo:
        return PaymentPrepaymentInfo(
            id=sp.id,
            kind=sp.kind,
            status=sp.status,
            amount=_float(sp.amount),
            amount_settled=_float(sp.amount_settled),
            settled_invoices=refs_for(prepay_allocs_by_id.get(sp.id, [])),
        )

    items: list[PaymentRegisterRow] = []
    for tx, wallet_name, article_name, cp_name in tx_rows:
        tx_allocs = [
            alloc
            for alloc in direct_allocs
            if alloc.cashflow_transaction_id == tx.id
            or (
                alloc.bank_operation_id is not None
                and bank_op_to_tx.get(alloc.bank_operation_id) == tx.id
            )
        ]
        sp = prepayment_by_tx.get(tx.id)
        direct_total = sum((periods.money(a.amount) for a in tx_allocs), Decimal("0"))
        covered = direct_total + (periods.money(sp.amount) if sp is not None else Decimal("0"))
        items.append(
            PaymentRegisterRow(
                id=tx.id,
                row_kind="transaction",
                operation_date=tx.operation_date,
                amount=_float(tx.amount),
                counterparty_id=tx.counterparty_id,
                counterparty_name=cp_name,
                wallet_name=wallet_name,
                article_name=article_name,
                purpose=tx.payment_purpose or tx.comment,
                settled_invoices=refs_for(tx_allocs),
                prepayment=prepayment_info(sp) if sp is not None else None,
                unassigned_amount=_float(_clamp_money(periods.money(tx.amount) - covered)),
            )
        )
    for sp, cp_name, article_name in opening_rows:
        items.append(
            PaymentRegisterRow(
                id=sp.id,
                row_kind="opening_prepayment",
                operation_date=sp.created_at.date(),
                amount=_float(sp.amount),
                counterparty_id=sp.counterparty_id,
                counterparty_name=cp_name,
                wallet_name=None,
                article_name=article_name,
                purpose=sp.note,
                settled_invoices=[],
                prepayment=prepayment_info(sp),
                unassigned_amount=0,
            )
        )

    items.sort(key=lambda row: row.operation_date, reverse=True)
    return PaymentRegisterList(items=items, total_amount=sum(row.amount for row in items))


@router.get("/documents", response_model=DocumentRegisterList, dependencies=READ)
async def list_document_register(
    session: Annotated[AsyncSession, Depends(get_session)],
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    counterparty_id: Annotated[uuid.UUID | None, Query()] = None,
) -> DocumentRegisterList:
    """Реестр УПД/накладных (закрывающие документы): пришёл → чем оплачен/погашен.

    Счета (bill) сюда НЕ входят — они не документы взаиморасчётов, а очередь оплат
    («Страница на оплату» / «Платежи»). Показываем и будущие УПД (activation='pending')."""
    filters = [
        SupplierInvoice.payment_status != "void",
        SupplierInvoice.direction == "payable",
        SupplierInvoice.doc_kind == "closing",
    ]
    if date_from is not None:
        filters.append(SupplierInvoice.invoice_date >= date_from)
    if date_to is not None:
        filters.append(SupplierInvoice.invoice_date <= date_to)
    if counterparty_id is not None:
        filters.append(SupplierInvoice.counterparty_id == counterparty_id)

    rows = (
        await session.execute(
            select(SupplierInvoice, Counterparty.name)
            .join(Counterparty, Counterparty.id == SupplierInvoice.counterparty_id)
            .where(*filters)
            .order_by(
                SupplierInvoice.invoice_date.desc().nulls_last(),
                SupplierInvoice.created_at.desc(),
            )
            .limit(1000)
        )
    ).all()

    invoice_ids = [row[0].id for row in rows]
    allocs: list[tuple[InvoicePaymentAllocation, str | None, date | None]] = []
    if invoice_ids:
        alloc_rows = (
            await session.execute(
                select(
                    InvoicePaymentAllocation,
                    SupplierPrepayment.kind,
                    CashflowTransaction.operation_date,
                )
                .outerjoin(
                    SupplierPrepayment,
                    SupplierPrepayment.id == InvoicePaymentAllocation.prepayment_id,
                )
                .outerjoin(
                    CashflowTransaction,
                    CashflowTransaction.id == InvoicePaymentAllocation.cashflow_transaction_id,
                )
                .where(InvoicePaymentAllocation.invoice_id.in_(invoice_ids))
                .order_by(InvoicePaymentAllocation.created_at)
            )
        ).all()
        allocs = [(row[0], row[1], row[2]) for row in alloc_rows]

    allocs_by_invoice: dict[uuid.UUID, list[DocumentAllocationRef]] = {}
    paid_by_invoice: dict[uuid.UUID, Decimal] = {}
    for alloc, prepayment_kind, tx_date in allocs:
        allocs_by_invoice.setdefault(alloc.invoice_id, []).append(
            DocumentAllocationRef(
                source_kind=alloc.source_kind,
                amount=_float(alloc.amount),
                operation_date=tx_date or alloc.created_at.date(),
                prepayment_kind=prepayment_kind,
            )
        )
        paid_by_invoice[alloc.invoice_id] = paid_by_invoice.get(
            alloc.invoice_id, Decimal("0")
        ) + periods.money(alloc.amount)

    # Бартерный заём гасится ТОВАРОМ: возвраты живут в леджере BarterReturnLine, а не в
    # аллокациях, поэтому «сумма − аллокации» показывала бы полный долг по уже возвращённому
    # займу — реестр расходился с плиткой «Остатки» на той же странице. Берём ТОЧНУЮ зачётную
    # стоимость (loan_settled_value: qty × исходная цена + замыкание «сумма строки — эталон»
    # против копеечного дрейфа округлений), а не SQL-приближение плитки. Для payable-займа она
    # УЖЕ включает денежные аллокации — поэтому paid к ней не добавляем, иначе двойной зачёт.
    from app.services.warehouse_invoices import loan_settled_value

    barter_settled: dict[uuid.UUID, Decimal] = {}
    for invoice, _cp_name in rows:
        if invoice.barter_role == "loan":
            barter_settled[invoice.id] = await loan_settled_value(session, invoice)
        elif invoice.barter_role == "return":
            # Возвратная накладная создаётся сразу 'paid' и аллокаций не несёт (её движение —
            # в леджере BarterReturnLine), иначе реестр рисует «Оплачено · остаток N».
            # Взаимозачёт сюда НЕ входит: с миграции 0199 он пишет аллокацию source_kind='barter',
            # и остаток по нему считается общим механизмом.
            barter_settled[invoice.id] = periods.money(invoice.amount)

    items: list[DocumentRegisterRow] = []
    for invoice, cp_name in rows:
        paid = (
            barter_settled[invoice.id]
            if invoice.id in barter_settled
            else paid_by_invoice.get(invoice.id, Decimal("0"))
        )
        items.append(
            DocumentRegisterRow(
                invoice_id=invoice.id,
                number=invoice.number,
                invoice_date=invoice.invoice_date,
                source=invoice.source,
                doc_kind=invoice.doc_kind,
                activation_status=invoice.activation_status,
                counterparty_id=invoice.counterparty_id,
                counterparty_name=cp_name,
                amount=_float(invoice.amount),
                payment_status=invoice.payment_status,
                remainder=_float(_clamp_money(periods.money(invoice.amount) - paid)),
                service_period_start=invoice.service_period_start,
                service_period_end=invoice.service_period_end,
                allocations=allocs_by_invoice.get(invoice.id, []),
            )
        )
    return DocumentRegisterList(
        items=items,
        total_amount=sum(row.amount for row in items),
        # Будущие УПД (pending) в кредиторку ещё не входят — из unpaid_total их исключаем.
        unpaid_total=sum(
            row.remainder
            for row in items
            if row.payment_status in UNPAID_INVOICE_STATUSES and row.activation_status == "active"
        ),
    )


class StaffPayableRow(BaseModel):
    employee_id: uuid.UUID
    full_name: str
    position: str | None = None
    basis: str
    earned_to_date: float
    already_advanced: float
    finalized_unpaid: float
    loans_outstanding: float
    payable: float
    receivable: float


class StaffPayableList(BaseModel):
    as_of: date
    total: float
    receivable_total: float
    items: list[StaffPayableRow]


async def _finalized_unpaid_by_employee(session: AsyncSession) -> dict[uuid.UUID, Decimal]:
    """Невыплаченные остатки ФИНАЛИЗИРОВАННЫХ ведомостей по сотрудникам.

    Берётся последний прогон каждого финализированного периода; долг = начислено
    (payroll_line.total_payable) − выплачено (payroll_payment.amount, бегущий итог).
    Легаси-заливка (is_imported_legacy) исключена: та история выплачена вне системы,
    иначе всплывают фантомные миллионы.
    """
    last_run = (
        select(PayrollRun.id)
        .join(PayrollPeriod, PayrollPeriod.id == PayrollRun.period_id)
        .where(
            PayrollPeriod.status == "finalized",
            PayrollRun.is_imported_legacy.is_(False),
        )
        .distinct(PayrollRun.period_id)
        .order_by(PayrollRun.period_id, PayrollRun.started_at.desc())
        .subquery()
    )
    accrued_rows = (
        await session.execute(
            select(
                PayrollLine.employee_id,
                func.sum(PayrollLine.total_payable),
            )
            .where(PayrollLine.run_id.in_(select(last_run.c.id)))
            .group_by(PayrollLine.employee_id)
        )
    ).all()
    paid_rows = (
        await session.execute(
            select(
                PayrollPayment.employee_id,
                func.sum(PayrollPayment.amount),
            )
            .where(
                PayrollPayment.run_id.in_(select(last_run.c.id)),
                PayrollPayment.status.in_(("paid", "partially_paid")),
            )
            .group_by(PayrollPayment.employee_id)
        )
    ).all()
    paid_by_emp = {row[0]: periods.money(row[1]) for row in paid_rows}
    debts: dict[uuid.UUID, Decimal] = {}
    for employee_id, accrued in accrued_rows:
        debt = periods.money(accrued) - paid_by_emp.get(employee_id, Decimal("0"))
        if debt > 0:
            debts[employee_id] = debt
    return debts


@router.get("/staff-payable", response_model=StaffPayableList, dependencies=READ)
async def list_staff_payable(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StaffPayableList:
    """Полный баланс с сотрудниками по канону: КЗ ← начисления, ДЗ ← выдачи сверх них.

    Кредиторка (мы должны) = заработанное за открытый период минус авансы периода
    (механизм «доступно к авансу»: провизорный прогон калькулятора по явкам / оклад
    по дням / смены — удержания уже в netto) ПЛЮС невыплаченные остатки финализированных
    ведомостей. Дебиторка (нам должны) = выдано за период сверх заработанного ПЛЮС
    непогашенные остатки займов/авансов, выданных ДО открытого периода (они удержатся
    будущими ведомостями). Займы внутри периода уже учтены в «выдано» — не задваиваются.
    Отдельная ручка: расчёт тяжёлый, дашборд грузит её асинхронно.
    """
    as_of = date.today()
    employees = (
        await session.scalars(
            select(Employee)
            .where(Employee.status.in_(("active", "dismissing")))
            .order_by(Employee.full_name)
        )
    ).all()

    finalized_unpaid = await _finalized_unpaid_by_employee(session)
    outstanding_rows = (
        await session.execute(
            select(
                SalaryAdvance.employee_id,
                SalaryAdvance.issued_on,
                SalaryAdvance.amount - SalaryAdvance.recovered_amount,
            ).where(
                SalaryAdvance.status == "issued",
                SalaryAdvance.amount > SalaryAdvance.recovered_amount,
            )
        )
    ).all()
    outstanding_by_emp: dict[uuid.UUID, list[tuple[date, Decimal]]] = {}
    for employee_id, issued_on, remainder in outstanding_rows:
        outstanding_by_emp.setdefault(employee_id, []).append((issued_on, periods.money(remainder)))

    # Границы ФИНАЛИЗИРОВАННЫХ ведомостных периодов: калькулятор доступного-к-авансу строит
    # СИНТЕТИЧЕСКИЙ «текущий» период по календарю (не глядя в PayrollPeriod), поэтому в день
    # финализации и после неё одни и те же деньги считались ДВАЖДЫ — как earned-to-date
    # синтетического периода и как невыплаченный хвост той же финализированной ведомости.
    finalized_bounds = {
        (row[0], row[1])
        for row in (
            await session.execute(
                select(PayrollPeriod.start_date, PayrollPeriod.end_date).where(
                    PayrollPeriod.status == "finalized"
                )
            )
        ).all()
    }

    items: list[StaffPayableRow] = []
    payable_total = Decimal("0.00")
    receivable_total = Decimal("0.00")
    for employee in employees:
        availability = await available_to_advance(session, employee, as_of)
        earned = periods.money(availability.earned_to_date)
        tail = finalized_unpaid.get(employee.id, Decimal("0"))
        period_start = availability.period_start
        # Период уже финализирован → его заработок ЦЕЛИКОМ несёт хвост ведомости (tail);
        # синтетический earned обнуляем, а внутрипериодные авансы уводим в «старые»: ведомость
        # их либо удержала (тогда их нет в outstanding), либо они выданы сверх рассчитанного —
        # это переаванс (дебиторка), не вычет из несуществующего earned.
        period_settled = (
            period_start is not None
            and availability.period_end is not None
            and (period_start, availability.period_end) in finalized_bounds
        )
        if period_settled:
            earned = Decimal("0.00")
        # Обе половины формулы делят авансы/займы ЕДИНЫМ фильтром outstanding_by_emp
        # (status='issued', непогашенный остаток). НЕ переиспользуем availability.already_advanced:
        # там фильтр `status != 'cancelled'`, т.е. в «выданное» протекают awaiting_payout (деньги
        # ещё НЕ выданы, банк-черновик/касса pending) и written_off (прощён) — они не живой долг и
        # искажали бы КЗ вниз, а ДЗ вверх фантомом. Внутрипериодные авансы уменьшают КЗ (аванс под
        # текущий заработок), выданные до периода — формируют ДЗ (займы-рассрочки, переавансы).
        in_period_advanced = sum(
            (
                remainder
                for issued_on, remainder in outstanding_by_emp.get(employee.id, [])
                if not period_settled and period_start is not None and issued_on >= period_start
            ),
            Decimal("0.00"),
        )
        old_outstanding = sum(
            (
                remainder
                for issued_on, remainder in outstanding_by_emp.get(employee.id, [])
                if period_settled or period_start is None or issued_on < period_start
            ),
            Decimal("0.00"),
        )
        payable = _clamp_money(earned - in_period_advanced) + tail
        receivable = _clamp_money(in_period_advanced - earned) + old_outstanding
        if payable <= 0 and receivable <= 0:
            continue
        payable_total += payable
        receivable_total += receivable
        items.append(
            StaffPayableRow(
                employee_id=employee.id,
                full_name=employee.full_name,
                position=employee.position,
                basis=availability.basis,
                earned_to_date=_float(earned),
                already_advanced=_float(in_period_advanced),
                finalized_unpaid=_float(tail),
                loans_outstanding=_float(old_outstanding),
                payable=_float(payable),
                receivable=_float(receivable),
            )
        )
    items.sort(key=lambda row: max(row.payable, row.receivable), reverse=True)
    return StaffPayableList(
        as_of=as_of,
        total=_float(payable_total),
        receivable_total=_float(receivable_total),
        items=items,
    )

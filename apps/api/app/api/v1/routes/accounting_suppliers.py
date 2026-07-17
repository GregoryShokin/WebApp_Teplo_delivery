"""Учёт ДЗ/КЗ поставщиков и журнал признания расходов по периодам услуг."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, ensure_permission, get_current_actor, require_permission
from app.db.session import get_session
from app.models import (
    CashflowTransaction,
    Counterparty,
    CounterpartyPaymentDraft,
    DdsArticle,
    Employee,
    InvoicePaymentAllocation,
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
            )
            .group_by(SupplierInvoice.counterparty_id)
        )
    ).all()

    # Контрагенты с закрытыми расчётами (0/0) остаются в списке: владелец видит ВСЕХ,
    # с кем есть документооборот, а не только должников.
    activity_rows = (
        await session.execute(
            select(
                SupplierInvoice.counterparty_id,
                func.max(SupplierInvoice.invoice_date),
            )
            .where(
                SupplierInvoice.payment_status != "void",
                SupplierInvoice.direction == "payable",
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

    receivable_by_cp = {row[0]: (periods.money(row[1]), row[2], row[3]) for row in prepay_rows}
    payable_by_cp = {row[0]: (periods.money(row[1]), row[2], row[3]) for row in invoice_rows}
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

    opening_filters = [SupplierPrepayment.cashflow_transaction_id.is_(None)]
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
    """Реестр УПД/накладных: документ пришёл → чем оплачен (предоплата/банк/касса)."""
    filters = [
        SupplierInvoice.payment_status != "void",
        SupplierInvoice.direction == "payable",
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

    items: list[DocumentRegisterRow] = []
    for invoice, cp_name in rows:
        paid = paid_by_invoice.get(invoice.id, Decimal("0"))
        items.append(
            DocumentRegisterRow(
                invoice_id=invoice.id,
                number=invoice.number,
                invoice_date=invoice.invoice_date,
                source=invoice.source,
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
        unpaid_total=sum(
            row.remainder for row in items if row.payment_status in UNPAID_INVOICE_STATUSES
        ),
    )


class StaffPayableRow(BaseModel):
    employee_id: uuid.UUID
    full_name: str
    position: str | None = None
    basis: str
    earned_to_date: float
    already_advanced: float
    payable: float


class StaffPayableList(BaseModel):
    as_of: date
    total: float
    items: list[StaffPayableRow]


@router.get("/staff-payable", response_model=StaffPayableList, dependencies=READ)
async def list_staff_payable(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StaffPayableList:
    """Долг перед сотрудниками на сегодня: заработано за открытый период − выданные авансы.

    Считает тем же механизмом, что «доступно к авансу» (earned-to-date: провизорный
    прогон недельного калькулятора по загруженным явкам / оклад по дням / смены мойщиц —
    депозиты, штрафы и удержания уже внутри netto). Это кредиторка, которая гасится
    выплатой ведомости. Отдельная ручка: расчёт тяжёлый, дашборд грузит её асинхронно.
    """
    as_of = date.today()
    employees = (
        await session.scalars(
            select(Employee)
            .where(Employee.status.in_(("active", "dismissing")))
            .order_by(Employee.full_name)
        )
    ).all()

    items: list[StaffPayableRow] = []
    total = Decimal("0.00")
    for employee in employees:
        availability = await available_to_advance(session, employee, as_of)
        payable = periods.money(availability.available)
        if payable <= 0:
            continue
        total += payable
        items.append(
            StaffPayableRow(
                employee_id=employee.id,
                full_name=employee.full_name,
                position=employee.position,
                basis=availability.basis,
                earned_to_date=_float(availability.earned_to_date),
                already_advanced=_float(availability.already_advanced),
                payable=_float(payable),
            )
        )
    items.sort(key=lambda row: row.payable, reverse=True)
    return StaffPayableList(as_of=as_of, total=_float(total), items=items)

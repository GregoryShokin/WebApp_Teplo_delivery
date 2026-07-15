"""Учёт ДЗ/КЗ поставщиков и журнал признания расходов по периодам услуг."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, ensure_permission, get_current_actor, require_permission
from app.db.session import get_session
from app.models import (
    Counterparty,
    CounterpartyPaymentDraft,
    DdsArticle,
    InvoicePaymentAllocation,
    SupplierExpenseAccrual,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services import supplier_service_periods as periods

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

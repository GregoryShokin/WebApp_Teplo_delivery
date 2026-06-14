"""«Управление складом» → накладные. Phase 1: nomenclature picker + manual cache sync.

Reuses the counterparties permission scopes on MVP (a dedicated warehouse.* scope can
be split out later). The picker defaults to GOODS — purchasable raw goods — so the
line-item product search isn't drowned in dishes/modifiers.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_permission
from app.db.session import get_session
from app.models import IikoProduct
from app.services.iiko_product_sync import sync_iiko_products
from app.services.warehouse_invoices import (
    LineInput,
    WarehouseInvoiceError,
    create_warehouse_invoice,
    get_warehouse_invoice,
    list_warehouse_invoices,
    next_invoice_number,
)

router = APIRouter()

READ = (Depends(require_permission("counterparties.read")),)
OPERATE = (Depends(require_permission("counterparties.operate")),)


class LineCreate(BaseModel):
    name: str
    quantity: Decimal
    price: Decimal
    iiko_product_id: uuid.UUID | None = None
    vat_percent: Decimal | None = None
    is_staff: bool = False


class InvoiceCreate(BaseModel):
    counterparty_id: uuid.UUID
    issued_at: datetime
    # "normal" — обычная приходная; "loan" — бартер-займ (we_lend: мы выдаём / нам выдают).
    mode: str = "normal"
    we_lend: bool = True
    number: str | None = None
    due_date: date | None = None
    store_guid: str | None = None
    lines: list[LineCreate] = Field(min_length=1)


@router.get("/products", dependencies=READ)
async def list_products(
    session: Annotated[AsyncSession, Depends(get_session)],
    q: str | None = None,
    type: str = "GOODS",
    include_deleted: bool = False,
    limit: int = Query(default=500, le=2000),
) -> list[dict[str, Any]]:
    """Nomenclature picker. ``type=''`` shows every type; defaults to GOODS."""
    query = select(IikoProduct)
    if type:
        query = query.where(IikoProduct.type == type.upper())
    if not include_deleted:
        query = query.where(IikoProduct.deleted.is_(False))
    if q:
        pattern = f"%{q.strip().lower()}%"
        query = query.where(
            func.lower(IikoProduct.name).like(pattern)
            | func.lower(func.coalesce(IikoProduct.code, "")).like(pattern)
        )
    query = query.order_by(IikoProduct.name).limit(limit)
    products = (await session.scalars(query)).all()
    return [
        {
            "id": str(p.id),
            "iiko_id": p.iiko_id,
            "name": p.name,
            "code": p.code,
            "unit": p.unit,
            "type": p.type,
        }
        for p in products
    ]


@router.post("/products/sync", dependencies=OPERATE)
async def post_products_sync(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Refresh the nomenclature cache from iiko `/v2/entities/products/list`."""
    result = await sync_iiko_products(session)
    return {
        "seen": result.seen,
        "created": result.created,
        "updated": result.updated,
        "goods_count": result.goods_count,
        "needs_unit_mapping": result.needs_unit_mapping,
    }


@router.get("/invoices/next-number", dependencies=READ)
async def invoice_next_number(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    return {"number": await next_invoice_number(session)}


@router.get("/invoices", dependencies=READ)
async def list_invoices(
    session: Annotated[AsyncSession, Depends(get_session)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    counterparty_id: uuid.UUID | None = None,
    has_staff: bool | None = None,
) -> list[dict[str, Any]]:
    statuses = [s for s in (status_filter or "").split(",") if s] or None
    return await list_warehouse_invoices(
        session, statuses=statuses, counterparty_id=counterparty_id, has_staff=has_staff
    )


@router.get("/invoices/{invoice_id}", dependencies=READ)
async def get_invoice(
    invoice_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    invoice = await get_warehouse_invoice(session, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Накладная не найдена")
    return invoice


@router.post("/invoices", dependencies=OPERATE, status_code=status.HTTP_201_CREATED)
async def post_invoice(
    payload: InvoiceCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    try:
        invoice = await create_warehouse_invoice(
            session,
            counterparty_id=payload.counterparty_id,
            issued_at=payload.issued_at,
            mode=payload.mode,
            we_lend=payload.we_lend,
            number=payload.number,
            due_date=payload.due_date,
            store_guid=payload.store_guid,
            lines=[
                LineInput(
                    name=line.name,
                    quantity=line.quantity,
                    price=line.price,
                    iiko_product_id=line.iiko_product_id,
                    vat_percent=line.vat_percent,
                    is_staff=line.is_staff,
                )
                for line in payload.lines
            ],
            actor_user_id=actor.user_id,
        )
    except WarehouseInvoiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    result = await get_warehouse_invoice(session, invoice.id)
    assert result is not None
    return result

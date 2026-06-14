"""«Управление складом» → накладные. Phase 1: nomenclature picker + manual cache sync.

Reuses the counterparties permission scopes on MVP (a dedicated warehouse.* scope can
be split out later). The picker defaults to GOODS — purchasable raw goods — so the
line-item product search isn't drowned in dishes/modifiers.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.db.session import get_session
from app.models import IikoProduct
from app.services.iiko_product_sync import sync_iiko_products

router = APIRouter()

READ = (Depends(require_permission("counterparties.read")),)
OPERATE = (Depends(require_permission("counterparties.operate")),)


@router.get("/products", dependencies=READ)
async def list_products(
    session: Annotated[AsyncSession, Depends(get_session)],
    q: str | None = None,
    type: str = "GOODS",
    include_deleted: bool = False,
    limit: int = Query(default=50, le=200),
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

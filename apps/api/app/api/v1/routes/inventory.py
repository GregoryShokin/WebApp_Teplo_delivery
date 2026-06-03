from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentActor, get_current_actor, require_finance_manager_plus
from app.db.session import get_session
from app.models import InventoryAudit, InventoryAuditItem, InventoryAuditPosition
from app.schemas.inventory import (
    IikoInventoryCandidateRead,
    IikoProductRead,
    InventoryAuditCreateManualPayload,
    InventoryAuditDetailRead,
    InventoryAuditImportIikoPayload,
    InventoryAuditItemCreate,
    InventoryAuditItemPatch,
    InventoryAuditListRead,
    InventoryAuditPatch,
    InventoryPositionCreate,
    InventoryPositionPatch,
    InventoryPositionRead,
    InventoryPositionsSyncRead,
    PenaltyComputationRead,
)
from app.services import iiko_inventory
from app.services.inventory_audit_service import (
    ALLOCATION_GROUPS,
    IikoInventoryDocumentNotFoundError,
    InventoryAuditConflictError,
    InventoryAuditNotFoundError,
    InventoryAuditValidationError,
    apply_audit_penalties,
    cancel_audit,
    compute_penalties,
    create_manual_audit,
    decimal_string,
    get_audit_with_items,
    import_audit_from_iiko,
    list_iiko_candidates,
    summarize_audit_items,
    sync_positions_from_iiko,
)

router = APIRouter()


@router.get("/positions", response_model=list[InventoryPositionRead])
async def list_positions(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
    stmt = select(InventoryAuditPosition)
    if not include_inactive:
        stmt = stmt.where(InventoryAuditPosition.is_active.is_(True))
    positions = (
        await session.scalars(
            stmt.order_by(InventoryAuditPosition.sort_order, InventoryAuditPosition.display_name)
        )
    ).all()
    return [position_payload(position) for position in positions]


@router.post("/positions", response_model=InventoryPositionRead)
async def create_position(
    payload: InventoryPositionCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    require_finance_manager_plus(actor)
    validate_allocation_group(payload.allocation_group)
    is_active = payload.allocation_group is not None
    position = InventoryAuditPosition(
        code=payload.code.strip(),
        display_name=payload.display_name.strip(),
        allocation_group=payload.allocation_group,
        iiko_product_guid=clean_optional_text(payload.iiko_product_guid),
        sort_order=payload.sort_order,
        is_active=is_active,
    )
    session.add(position)
    await session.commit()
    await session.refresh(position)
    return position_payload(position)


@router.post("/positions/sync-iiko", response_model=InventoryPositionsSyncRead)
async def sync_positions_iiko(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, int]:
    require_finance_manager_plus(actor)
    result = await sync_positions_from_iiko(session, actor=actor)
    return {"added": result.added, "updated": result.updated, "total": result.total}


@router.patch("/positions/{position_id}", response_model=InventoryPositionRead)
async def patch_position(
    position_id: uuid.UUID,
    payload: InventoryPositionPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    require_finance_manager_plus(actor)
    position = await position_or_404(session, position_id)
    updates = payload.model_dump(exclude_unset=True)
    if "display_name" in updates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Название синхронизируется из iiko, его нельзя изменить вручную",
        )
    target_is_active = updates.get("is_active", position.is_active)
    target_group = updates.get("allocation_group", position.allocation_group)
    if target_is_active is True and target_group is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Перед активацией укажите группу распределения штрафа",
        )
    for field, value in updates.items():
        if field == "allocation_group":
            validate_allocation_group(value)
        setattr(position, field, value)
    position.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(position)
    return position_payload(position)


@router.get("/iiko-products", response_model=list[IikoProductRead])
async def list_iiko_products(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
    search: str = "",
    refresh: bool = False,
) -> list[dict[str, str | None]]:
    products = await iiko_inventory.fetch_products_catalog(session, refresh=refresh)
    needle = search.strip().casefold()
    rows: list[dict[str, str | None]] = []
    for product in products.values():
        row = {
            "guid": str(product.get("guid") or product.get("id") or ""),
            "name": str(product.get("name") or ""),
            "code": str(product.get("code") or "") or None,
        }
        haystack = f"{row['guid']} {row['name']} {row['code'] or ''}".casefold()
        if needle and needle not in haystack:
            continue
        if row["guid"]:
            rows.append(row)
    return sorted(rows, key=lambda item: (item["name"] or "", item["guid"]))[:100]


@router.get("/audits", response_model=list[InventoryAuditListRead])
async def list_audits(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
    date_from: date | None = None,
    date_to: date | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> list[dict[str, Any]]:
    stmt = select(InventoryAudit)
    if date_from is not None:
        stmt = stmt.where(InventoryAudit.business_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(InventoryAudit.business_date <= date_to)
    if status_filter:
        stmt = stmt.where(InventoryAudit.status == status_filter)
    audits = (await session.scalars(stmt.order_by(InventoryAudit.business_date.desc()))).all()
    return [audit_payload(audit) for audit in audits]


@router.get("/audits/iiko-candidates", response_model=list[IikoInventoryCandidateRead])
async def iiko_audit_candidates(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
    business_date: date,
) -> list[dict[str, Any]]:
    return await list_iiko_candidates(session, business_date=business_date)


@router.post("/audits/import-iiko", response_model=InventoryAuditDetailRead)
async def import_iiko_audit(
    payload: InventoryAuditImportIikoPayload,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    require_finance_manager_plus(actor)
    try:
        audit = await import_audit_from_iiko(
            session,
            business_date=payload.business_date,
            document_id=payload.document_id,
            actor=actor,
        )
    except IikoInventoryDocumentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Документ инвентаризации не найден в iiko за эту дату",
        ) from exc
    except InventoryAuditConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return audit_payload(audit, include_items=True)


@router.post("/audits", response_model=InventoryAuditDetailRead)
async def create_audit(
    payload: InventoryAuditCreateManualPayload,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    require_finance_manager_plus(actor)
    try:
        audit = await create_manual_audit(
            session,
            business_date=payload.business_date,
            items=[item.model_dump() for item in payload.items],
            notes=payload.notes,
            actor=actor,
        )
    except InventoryAuditConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except InventoryAuditValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return audit_payload(audit, include_items=True)


@router.get("/audits/{audit_id}", response_model=InventoryAuditDetailRead)
async def get_audit(
    audit_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    try:
        result = await get_audit_with_items(session, audit_id)
    except InventoryAuditNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return audit_payload(result["audit"], include_items=True)


@router.patch("/audits/{audit_id}", response_model=InventoryAuditDetailRead)
async def patch_audit(
    audit_id: uuid.UUID,
    payload: InventoryAuditPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    require_finance_manager_plus(actor)
    audit = await load_audit_or_404(session, audit_id)
    audit.notes = clean_optional_text(payload.notes)
    await session.commit()
    await session.refresh(audit)
    audit = await load_audit_or_404(session, audit.id)
    return audit_payload(audit, include_items=True)


@router.post("/audits/{audit_id}/items", response_model=InventoryAuditDetailRead)
async def add_audit_item(
    audit_id: uuid.UUID,
    payload: InventoryAuditItemCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    require_finance_manager_plus(actor)
    audit = await load_audit_or_404(session, audit_id)
    ensure_draft(audit)
    position = await position_from_payload(session, payload.model_dump())
    name = payload.product_name_snapshot or (position.display_name if position else None)
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Укажите позицию или название товара",
        )
    amount = payload.shortage_amount
    session.add(
        InventoryAuditItem(
            audit_id=audit.id,
            position_id=position.id if position else None,
            iiko_product_guid=clean_optional_text(payload.iiko_product_guid),
            product_name_snapshot=name.strip(),
            shortage_amount=amount,
        )
    )
    reset_computation(audit, total=Decimal(str(audit.total_shortage_amount)) + amount)
    await session.commit()
    return audit_payload(await load_audit_or_404(session, audit.id), include_items=True)


@router.patch("/audits/{audit_id}/items/{item_id}", response_model=InventoryAuditDetailRead)
async def patch_audit_item(
    audit_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: InventoryAuditItemPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    require_finance_manager_plus(actor)
    audit = await load_audit_or_404(session, audit_id)
    ensure_draft(audit)
    item = next((candidate for candidate in audit.items if candidate.id == item_id), None)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Позиция не найдена")

    updates = payload.model_dump(exclude_unset=True)
    if "position_id" in updates or "position_code" in updates:
        position = await position_from_payload(session, updates)
        item.position_id = position.id if position else None
        if position and "product_name_snapshot" not in updates:
            item.product_name_snapshot = position.display_name
    if "iiko_product_guid" in updates:
        item.iiko_product_guid = clean_optional_text(updates["iiko_product_guid"])
    if "product_name_snapshot" in updates:
        item.product_name_snapshot = str(updates["product_name_snapshot"]).strip()
    if "shortage_amount" in updates:
        item.shortage_amount = updates["shortage_amount"]
    reset_computation(audit)
    await session.commit()
    return audit_payload(await load_audit_or_404(session, audit.id), include_items=True)


@router.delete("/audits/{audit_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_audit_item(
    audit_id: uuid.UUID,
    item_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> Response:
    require_finance_manager_plus(actor)
    audit = await load_audit_or_404(session, audit_id)
    ensure_draft(audit)
    item = next((candidate for candidate in audit.items if candidate.id == item_id), None)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Позиция не найдена")
    next_total = Decimal(str(audit.total_shortage_amount)) - Decimal(str(item.shortage_amount))
    await session.delete(item)
    reset_computation(audit, total=next_total)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/audits/{audit_id}/compute", response_model=PenaltyComputationRead)
async def compute_audit(
    audit_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    require_finance_manager_plus(actor)
    try:
        computation = await compute_penalties(session, audit_id)
    except InventoryAuditNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return computation_payload(computation)


@router.post("/audits/{audit_id}/apply", response_model=InventoryAuditDetailRead)
async def apply_audit(
    audit_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    require_finance_manager_plus(actor)
    try:
        audit = await apply_audit_penalties(session, audit_id, actor=actor)
    except InventoryAuditNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InventoryAuditConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return audit_payload(audit, include_items=True)


@router.post("/audits/{audit_id}/cancel", response_model=InventoryAuditDetailRead)
async def cancel_inventory_audit(
    audit_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    require_finance_manager_plus(actor)
    try:
        audit = await cancel_audit(session, audit_id, actor=actor)
    except InventoryAuditNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InventoryAuditConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return audit_payload(audit, include_items=True)


async def load_audit_or_404(session: AsyncSession, audit_id: uuid.UUID) -> InventoryAudit:
    audit = await session.scalar(
        select(InventoryAudit)
        .options(selectinload(InventoryAudit.items).selectinload(InventoryAuditItem.position))
        .where(InventoryAudit.id == audit_id)
    )
    if audit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ревизия не найдена")
    return audit


async def position_or_404(
    session: AsyncSession,
    position_id: uuid.UUID,
) -> InventoryAuditPosition:
    position = await session.get(InventoryAuditPosition, position_id)
    if position is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Позиция не найдена")
    return position


async def position_from_payload(
    session: AsyncSession,
    payload: dict[str, Any],
) -> InventoryAuditPosition | None:
    position_id = payload.get("position_id")
    position_code = payload.get("position_code")
    if position_id:
        return await position_or_404(session, position_id)
    if position_code:
        position = await session.scalar(
            select(InventoryAuditPosition).where(InventoryAuditPosition.code == position_code)
        )
        if position is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Позиция не найдена")
        return position
    return None


def ensure_draft(audit: InventoryAudit) -> None:
    if audit.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Изменять позиции можно только в черновике",
        )


def reset_computation(audit: InventoryAudit, total: Decimal | None = None) -> None:
    audit.total_shortage_amount = (
        total
        if total is not None
        else sum(
            (Decimal(str(item.shortage_amount)) for item in audit.items),
            Decimal("0"),
        )
    )
    audit.total_penalty_amount = Decimal("0")
    audit.computation_snapshot = None


def audit_payload(audit: InventoryAudit, *, include_items: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": audit.id,
        "business_date": audit.business_date,
        "previous_audit_date": audit.previous_audit_date,
        "iiko_document_id": audit.iiko_document_id,
        "iiko_document_num": audit.iiko_document_num,
        "source": audit.source,
        "status": audit.status,
        "total_shortage_amount": decimal_string(audit.total_shortage_amount),
        "total_penalty_amount": decimal_string(audit.total_penalty_amount),
        "employee_count": snapshot_employee_count(audit.computation_snapshot),
        "notes": audit.notes,
        "created_at": audit.created_at,
        "updated_at": audit.updated_at,
        "applied_at": audit.applied_at,
    }
    if include_items:
        summary = summarize_audit_items(list(audit.items))
        payload["total_shortage_iiko"] = decimal_string(summary.total_shortage_iiko)
        payload["total_shortage_considered"] = decimal_string(summary.total_shortage_considered)
        payload["items"] = [item_payload(item) for item in summary.considered_items]
        payload["items_skipped_count"] = summary.items_skipped_count
        payload["computation_snapshot"] = audit.computation_snapshot
    return payload


def item_payload(item: InventoryAuditItem) -> dict[str, Any]:
    position = item.position
    return {
        "id": item.id,
        "audit_id": item.audit_id,
        "position_id": item.position_id,
        "position_code": position.code if position else None,
        "position_display_name": position.display_name if position else None,
        "allocation_group": position.allocation_group if position else None,
        "is_considered": position is not None
        and position.is_active is True
        and position.allocation_group is not None,
        "iiko_product_guid": item.iiko_product_guid,
        "product_name_snapshot": item.product_name_snapshot,
        "shortage_amount": decimal_string(item.shortage_amount),
        "created_at": item.created_at,
    }


def position_payload(position: InventoryAuditPosition) -> dict[str, Any]:
    return {
        "id": position.id,
        "code": position.code,
        "display_name": position.display_name,
        "allocation_group": position.allocation_group,
        "iiko_product_guid": position.iiko_product_guid,
        "is_active": position.is_active,
        "sort_order": position.sort_order,
        "created_at": position.created_at,
        "updated_at": position.updated_at,
    }


def computation_payload(computation: Any) -> dict[str, Any]:
    return {
        "audit_id": computation.audit_id,
        "total_shortage_amount": decimal_string(computation.total_shortage_amount),
        "total_penalty_amount": decimal_string(computation.total_penalty_amount),
        "period_start": computation.period_start,
        "period_end": computation.period_end,
        "groups": computation.groups,
        "employee_penalties": computation.employee_rows,
        "warnings": computation.warnings,
    }


def snapshot_employee_count(snapshot: dict[str, Any] | None) -> int:
    if not isinstance(snapshot, dict):
        return 0
    rows = snapshot.get("employee_penalties")
    if isinstance(rows, list):
        return len(rows)
    by_id = snapshot.get("employee_penalties_by_id")
    if isinstance(by_id, dict):
        return len(by_id)
    return 0


def validate_allocation_group(value: str | None) -> None:
    if value is not None and value not in ALLOCATION_GROUPS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Некорректная группа распределения",
        )


def clean_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

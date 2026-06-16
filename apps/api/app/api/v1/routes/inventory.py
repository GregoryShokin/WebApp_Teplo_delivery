from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentActor, get_current_actor, require_permission
from app.db.session import get_session
from app.models import (
    InventoryAudit,
    InventoryAuditEmployeeExclusion,
    InventoryAuditItem,
    InventoryAuditItemExclusion,
    InventoryAuditPosition,
)
from app.schemas.inventory import (
    DeferredOnPayoutRead,
    IikoInventoryCandidateRead,
    IikoProductRead,
    InventoryAuditAllExclusionsRead,
    InventoryAuditCreateManualPayload,
    InventoryAuditDetailRead,
    InventoryAuditEmployeeExclusionPatch,
    InventoryAuditImportIikoPayload,
    InventoryAuditItemCreate,
    InventoryAuditItemExclusionPatch,
    InventoryAuditItemPatch,
    InventoryAuditListRead,
    InventoryAuditPatch,
    InventoryPositionCreate,
    InventoryPositionPatch,
    InventoryPositionRead,
    InventoryPositionsSyncRead,
    PayoutOptionRead,
    PenaltyComputationRead,
)
from app.services import iiko_inventory
from app.services.deferred_audit_charge_service import (
    deferred_splits_on_payout,
    list_deferred_charges,
)
from app.services.inventory_audit_service import (
    ALLOCATION_GROUPS,
    IikoInventoryDocumentNotFoundError,
    InventoryAuditConflictError,
    InventoryAuditNotFoundError,
    InventoryAuditValidationError,
    apply_audit_penalties,
    audit_item_is_considered,
    cancel_audit,
    compute_penalties,
    create_manual_audit,
    decimal_string,
    effective_penalty_work_date,
    get_audit_with_items,
    import_audit_from_iiko,
    item_effective_swap_group,
    item_has_swap_group_override,
    item_shortage_amount,
    item_signed_amount,
    item_swap_group_override,
    list_all_exclusions,
    list_iiko_candidates,
    list_payout_options,
    payroll_period_for_date,
    restore_cancelled_audit,
    set_employee_exclusion,
    set_item_exclusion,
    set_penalty_work_date_override,
    summarize_audit_items,
    summarize_swap_groups,
    sync_positions_from_iiko,
    write_swap_override_audit,
)

router = APIRouter()
SOURCE_REVISION_SETTINGS_READ_ACCESS = (
    Depends(require_permission("source.revision_settings.read")),
)
SOURCE_REVISION_SETTINGS_EDIT_ACCESS = (
    Depends(require_permission("source.revision_settings.edit")),
)
REVISION_READ_ACCESS = (Depends(require_permission("revisions.read")),)
REVISION_IMPORT_ACCESS = (Depends(require_permission("revisions.import")),)
REVISION_CREATE_ACCESS = (Depends(require_permission("revisions.create")),)
REVISION_DRAFT_EDIT_ACCESS = (Depends(require_permission("revisions.drafts.edit")),)
REVISION_ITEM_EXCLUDE_ACCESS = (Depends(require_permission("revisions.items.exclude")),)
REVISION_EMPLOYEE_EXCLUDE_ACCESS = (Depends(require_permission("revisions.employees.exclude")),)
REVISION_FINALIZE_ACCESS = (Depends(require_permission("revisions.finalize")),)
REVISION_CANCEL_ACCESS = (Depends(require_permission("revisions.cancel")),)
REVISION_RESTORE_DRAFT_ACCESS = (Depends(require_permission("revisions.restore_draft")),)


@router.get(
    "/positions",
    response_model=list[InventoryPositionRead],
    dependencies=SOURCE_REVISION_SETTINGS_READ_ACCESS,
)
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


@router.post(
    "/positions",
    response_model=InventoryPositionRead,
    dependencies=SOURCE_REVISION_SETTINGS_EDIT_ACCESS,
)
async def create_position(
    payload: InventoryPositionCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    validate_allocation_group(payload.allocation_group)
    swap_group = clean_swap_group_input(payload.swap_group)
    await validate_swap_group_allocation(
        session,
        swap_group=swap_group,
        allocation_group=payload.allocation_group,
        current_position_id=None,
        current_position_name=payload.display_name.strip(),
    )
    is_active = payload.allocation_group is not None
    position = InventoryAuditPosition(
        code=payload.code.strip(),
        display_name=payload.display_name.strip(),
        allocation_group=payload.allocation_group,
        swap_group=swap_group,
        iiko_product_guid=clean_optional_text(payload.iiko_product_guid),
        sort_order=payload.sort_order,
        is_active=is_active,
    )
    session.add(position)
    await session.commit()
    await session.refresh(position)
    return position_payload(position)


@router.post(
    "/positions/sync-iiko",
    response_model=InventoryPositionsSyncRead,
    dependencies=SOURCE_REVISION_SETTINGS_EDIT_ACCESS,
)
async def sync_positions_iiko(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, int]:
    result = await sync_positions_from_iiko(session, actor=actor)
    return {"added": result.added, "updated": result.updated, "total": result.total}


@router.patch(
    "/positions/{position_id}",
    response_model=InventoryPositionRead,
    dependencies=SOURCE_REVISION_SETTINGS_EDIT_ACCESS,
)
async def patch_position(
    position_id: uuid.UUID,
    payload: InventoryPositionPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    position = await position_or_404(session, position_id)
    updates = payload.model_dump(exclude_unset=True)
    if "display_name" in updates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Название синхронизируется из iiko, его нельзя изменить вручную",
        )
    target_is_active = updates.get("is_active", position.is_active)
    target_group = updates.get("allocation_group", position.allocation_group)
    target_swap_group = (
        clean_swap_group_input(updates["swap_group"])
        if "swap_group" in updates
        else getattr(position, "swap_group", None)
    )
    if target_is_active is True and target_group is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Перед активацией укажите группу распределения штрафа",
        )
    if "swap_group" in updates or "allocation_group" in updates:
        await validate_swap_group_allocation(
            session,
            swap_group=target_swap_group,
            allocation_group=target_group,
            current_position_id=position.id,
            current_position_name=position.display_name,
        )
    for field, value in updates.items():
        if field == "allocation_group":
            validate_allocation_group(value)
        if field == "swap_group":
            value = clean_swap_group_input(value)
        setattr(position, field, value)
    position.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(position)
    return position_payload(position)


@router.get(
    "/iiko-products",
    response_model=list[IikoProductRead],
    dependencies=SOURCE_REVISION_SETTINGS_READ_ACCESS,
)
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


@router.get(
    "/audit-exclusions",
    response_model=InventoryAuditAllExclusionsRead,
    dependencies=REVISION_READ_ACCESS,
)
async def list_audit_exclusions(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    audit_date_from: date | None = None,
    audit_date_to: date | None = None,
    employee_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    return await list_all_exclusions(
        session,
        audit_date_from=audit_date_from,
        audit_date_to=audit_date_to,
        employee_id=employee_id,
    )


@router.get(
    "/audits",
    response_model=list[InventoryAuditListRead],
    dependencies=REVISION_READ_ACCESS,
)
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


@router.get(
    "/audits/iiko-candidates",
    response_model=list[IikoInventoryCandidateRead],
    dependencies=REVISION_READ_ACCESS,
)
async def iiko_audit_candidates(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
    business_date: date,
) -> list[dict[str, Any]]:
    return await list_iiko_candidates(session, business_date=business_date)


@router.post(
    "/audits/import-iiko",
    response_model=InventoryAuditDetailRead,
    dependencies=REVISION_IMPORT_ACCESS,
)
async def import_iiko_audit(
    payload: InventoryAuditImportIikoPayload,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
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


@router.post(
    "/audits",
    response_model=InventoryAuditDetailRead,
    dependencies=REVISION_CREATE_ACCESS,
)
async def create_audit(
    payload: InventoryAuditCreateManualPayload,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
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


@router.get(
    "/audits/{audit_id}",
    response_model=InventoryAuditDetailRead,
    dependencies=REVISION_READ_ACCESS,
)
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


@router.get(
    "/audits/{audit_id}/preview",
    response_model=InventoryAuditDetailRead,
    dependencies=REVISION_READ_ACCESS,
)
async def preview_audit(
    audit_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    try:
        await compute_penalties(session, audit_id)
        result = await get_audit_with_items(session, audit_id)
    except InventoryAuditNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return audit_payload(result["audit"], include_items=True)


@router.patch(
    "/audits/{audit_id}",
    response_model=InventoryAuditDetailRead,
    dependencies=REVISION_DRAFT_EDIT_ACCESS,
)
async def patch_audit(
    audit_id: uuid.UUID,
    payload: InventoryAuditPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    audit = await load_audit_or_404(session, audit_id)
    fields = payload.model_fields_set
    if "notes" in fields:
        audit.notes = clean_optional_text(payload.notes)
    if "penalty_work_date_override" in fields:
        try:
            await set_penalty_work_date_override(
                session,
                audit,
                payload.penalty_work_date_override,
                actor=actor,
            )
        except InventoryAuditConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc
        except InventoryAuditValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
    await session.commit()
    audit = await load_audit_or_404(session, audit.id)
    return audit_payload(audit, include_items=True)


@router.get(
    "/audits/{audit_id}/payout-options",
    response_model=list[PayoutOptionRead],
    dependencies=REVISION_READ_ACCESS,
)
async def audit_payout_options(
    audit_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[dict[str, Any]]:
    audit = await load_audit_or_404(session, audit_id)
    return await list_payout_options(session, audit)


@router.get(
    "/audits/{audit_id}/deferred-on-payout",
    response_model=list[DeferredOnPayoutRead],
    dependencies=REVISION_READ_ACCESS,
)
async def audit_deferred_on_payout(
    audit_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[dict[str, Any]]:
    audit = await load_audit_or_404(session, audit_id)
    period_start, _period_end, _payout = payroll_period_for_date(
        effective_penalty_work_date(audit)
    )
    charges = await list_deferred_charges(session)
    return deferred_splits_on_payout(charges, period_start=period_start)


@router.post(
    "/audits/{audit_id}/items",
    response_model=InventoryAuditDetailRead,
    dependencies=REVISION_DRAFT_EDIT_ACCESS,
)
async def add_audit_item(
    audit_id: uuid.UUID,
    payload: InventoryAuditItemCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    audit = await load_audit_or_404(session, audit_id)
    ensure_draft(audit)
    position = await position_from_payload(session, payload.model_dump())
    name = payload.product_name_snapshot or (position.display_name if position else None)
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Укажите позицию или название товара",
        )
    shortage_amount = payload.shortage_amount
    session.add(
        InventoryAuditItem(
            audit_id=audit.id,
            position_id=position.id if position else None,
            iiko_product_guid=clean_optional_text(payload.iiko_product_guid),
            product_name_snapshot=name.strip(),
            shortage_amount=shortage_amount,
            amount=-shortage_amount,
        )
    )
    reset_computation(audit, total=Decimal(str(audit.total_shortage_amount)) + shortage_amount)
    await session.commit()
    return audit_payload(await load_audit_or_404(session, audit.id), include_items=True)


@router.patch(
    "/audits/{audit_id}/items/{item_id}",
    response_model=InventoryAuditDetailRead,
    dependencies=REVISION_DRAFT_EDIT_ACCESS,
)
async def patch_audit_item(
    audit_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: InventoryAuditItemPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    audit = await load_audit_or_404(session, audit_id)
    ensure_draft(audit)
    item = next((candidate for candidate in audit.items if candidate.id == item_id), None)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Позиция не найдена")

    updates = payload.model_dump(exclude_unset=True)
    before_swap_override = getattr(item, "swap_group_override", None)
    before_effective_swap_group = item_effective_swap_group(item)
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
        item.amount = -updates["shortage_amount"]
    if "swap_group_override" in updates:
        item.swap_group_override = clean_swap_group_override(updates["swap_group_override"])
        validate_audit_item_swap_override(audit, item)
        if item.swap_group_override != before_swap_override:
            await write_swap_override_audit(
                session,
                audit=audit,
                item=item,
                actor=actor,
                before={
                    "swap_group_override": before_swap_override,
                    "effective_swap_group": before_effective_swap_group,
                },
                after={
                    "swap_group_override": item.swap_group_override,
                    "effective_swap_group": item_effective_swap_group(item),
                },
            )
    reset_computation(audit)
    await session.commit()
    return audit_payload(await load_audit_or_404(session, audit.id), include_items=True)


@router.delete(
    "/audits/{audit_id}/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=REVISION_DRAFT_EDIT_ACCESS,
)
async def delete_audit_item(
    audit_id: uuid.UUID,
    item_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> Response:
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


@router.patch(
    "/audits/{audit_id}/employee-exclusions/{employee_id}",
    response_model=InventoryAuditDetailRead,
    dependencies=REVISION_EMPLOYEE_EXCLUDE_ACCESS,
)
async def patch_audit_employee_exclusion(
    audit_id: uuid.UUID,
    employee_id: uuid.UUID,
    payload: InventoryAuditEmployeeExclusionPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    try:
        audit = await set_employee_exclusion(
            session,
            audit_id,
            employee_id,
            excluded=payload.excluded,
            reason=payload.reason,
            actor=actor,
        )
    except InventoryAuditNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InventoryAuditConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except InventoryAuditValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return audit_payload(audit, include_items=True)


@router.patch(
    "/audits/{audit_id}/items/{item_id}/exclusion",
    response_model=InventoryAuditDetailRead,
    dependencies=REVISION_ITEM_EXCLUDE_ACCESS,
)
async def patch_audit_item_exclusion(
    audit_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: InventoryAuditItemExclusionPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    try:
        audit = await set_item_exclusion(
            session,
            audit_id,
            item_id,
            excluded=payload.excluded,
            reason=payload.reason,
            actor=actor,
        )
    except InventoryAuditNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InventoryAuditConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except InventoryAuditValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return audit_payload(audit, include_items=True)


@router.post(
    "/audits/{audit_id}/compute",
    response_model=PenaltyComputationRead,
    dependencies=REVISION_READ_ACCESS,
)
async def compute_audit(
    audit_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    try:
        computation = await compute_penalties(session, audit_id)
    except InventoryAuditNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return computation_payload(computation)


@router.post(
    "/audits/{audit_id}/apply",
    response_model=InventoryAuditDetailRead,
    dependencies=REVISION_FINALIZE_ACCESS,
)
async def apply_audit(
    audit_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    try:
        audit = await apply_audit_penalties(session, audit_id, actor=actor)
    except InventoryAuditNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InventoryAuditConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return audit_payload(audit, include_items=True)


@router.post(
    "/audits/{audit_id}/cancel",
    response_model=InventoryAuditDetailRead,
    dependencies=REVISION_CANCEL_ACCESS,
)
async def cancel_inventory_audit(
    audit_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    try:
        audit = await cancel_audit(session, audit_id, actor=actor)
    except InventoryAuditNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InventoryAuditConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return audit_payload(audit, include_items=True)


@router.post(
    "/audits/{audit_id}/restore-draft",
    response_model=InventoryAuditDetailRead,
    dependencies=REVISION_RESTORE_DRAFT_ACCESS,
)
async def restore_inventory_audit_draft(
    audit_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    try:
        audit = await restore_cancelled_audit(session, audit_id, actor=actor)
    except InventoryAuditNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InventoryAuditConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return audit_payload(audit, include_items=True)


async def load_audit_or_404(session: AsyncSession, audit_id: uuid.UUID) -> InventoryAudit:
    audit = await session.scalar(
        select(InventoryAudit)
        .options(
            selectinload(InventoryAudit.items).selectinload(InventoryAuditItem.position),
            selectinload(InventoryAudit.employee_exclusions).selectinload(
                InventoryAuditEmployeeExclusion.employee
            ),
            selectinload(InventoryAudit.employee_exclusions).selectinload(
                InventoryAuditEmployeeExclusion.created_by
            ),
            selectinload(InventoryAudit.item_exclusions).selectinload(
                InventoryAuditItemExclusion.item
            ),
            selectinload(InventoryAudit.item_exclusions).selectinload(
                InventoryAuditItemExclusion.created_by
            ),
        )
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
            (item_shortage_amount(item) for item in audit.items),
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
        "penalty_work_date_override": getattr(audit, "penalty_work_date_override", None),
        "penalty_work_date_effective": effective_penalty_work_date(audit),
        "created_at": audit.created_at,
        "updated_at": audit.updated_at,
        "applied_at": audit.applied_at,
    }
    if include_items:
        item_exclusions = item_exclusion_reasons(audit)
        excluded_ids = set(item_exclusions)
        summary = summarize_audit_items(list(audit.items), excluded_ids=excluded_ids)
        payload["total_shortage_iiko"] = decimal_string(summary.total_shortage_iiko)
        payload["total_shortage_considered"] = decimal_string(
            effective_total_shortage_considered(audit, summary.total_shortage_considered)
        )
        visible_items = [
            item
            for item in summary.all_items
            if getattr(item, "id", None) in excluded_ids or audit_item_is_considered(item)
        ]
        payload["items"] = [
            item_payload(item, exclusion_reason=item_exclusions.get(item.id))
            for item in visible_items
        ]
        snapshot_swap_groups = (
            audit.computation_snapshot.get("swap_groups")
            if isinstance(audit.computation_snapshot, dict)
            else None
        )
        payload["swap_groups"] = (
            snapshot_swap_groups
            if isinstance(snapshot_swap_groups, list)
            else summarize_swap_groups(summary.considered_items)
        )
        payload["items_skipped_count"] = summary.items_skipped_count
        payload["computation_snapshot"] = audit.computation_snapshot
        payload["item_exclusions_log"] = [
            {
                "id": str(exclusion.id),
                "item_id": str(exclusion.item_id),
                "product_name": (
                    getattr(exclusion.item, "product_name_snapshot", None)
                    if exclusion.item is not None
                    else None
                ),
                "amount": (
                    decimal_string(item_signed_amount(exclusion.item))
                    if exclusion.item is not None
                    else None
                ),
                "reason": exclusion.reason,
                "created_by_name": (
                    exclusion.created_by.full_name if exclusion.created_by is not None else None
                ),
                "created_at": exclusion.created_at,
                "updated_at": exclusion.updated_at,
            }
            for exclusion in getattr(audit, "item_exclusions", [])
        ]
        payload["employee_exclusions_log"] = [
            {
                "id": str(exclusion.id),
                "employee_id": str(exclusion.employee_id),
                "employee_name": (
                    exclusion.employee.full_name if exclusion.employee is not None else None
                ),
                "employee_position": (
                    getattr(exclusion.employee, "position", None)
                    if exclusion.employee is not None
                    else None
                ),
                "reason": exclusion.reason,
                "created_by_name": (
                    exclusion.created_by.full_name if exclusion.created_by is not None else None
                ),
                "created_at": exclusion.created_at,
                "updated_at": exclusion.updated_at,
            }
            for exclusion in getattr(audit, "employee_exclusions", [])
        ]
    return payload


def item_payload(
    item: InventoryAuditItem,
    *,
    exclusion_reason: str | None = None,
) -> dict[str, Any]:
    position = item.position
    is_excluded = exclusion_reason is not None
    return {
        "id": item.id,
        "audit_id": item.audit_id,
        "position_id": item.position_id,
        "position_code": position.code if position else None,
        "position_display_name": position.display_name if position else None,
        "allocation_group": position.allocation_group if position else None,
        "is_considered": audit_item_is_considered(
            item,
            excluded_ids={item.id} if is_excluded else None,
        ),
        "is_excluded": is_excluded,
        "exclusion_reason": exclusion_reason,
        "amount": decimal_string(item_signed_amount(item)),
        "swap_group": item_effective_swap_group(item),
        "swap_group_default": getattr(position, "swap_group", None) if position else None,
        "swap_group_override": item_swap_group_override(item),
        "has_swap_group_override": item_has_swap_group_override(item),
        "iiko_product_guid": item.iiko_product_guid,
        "product_name_snapshot": item.product_name_snapshot,
        "shortage_amount": decimal_string(item.shortage_amount),
        "created_at": item.created_at,
    }


def item_exclusion_reasons(audit: InventoryAudit) -> dict[uuid.UUID, str]:
    return {
        exclusion.item_id: exclusion.reason for exclusion in getattr(audit, "item_exclusions", [])
    }


def position_payload(position: InventoryAuditPosition) -> dict[str, Any]:
    return {
        "id": position.id,
        "code": position.code,
        "display_name": position.display_name,
        "allocation_group": position.allocation_group,
        "swap_group": getattr(position, "swap_group", None),
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
        "swap_groups": computation.swap_groups,
        "employee_penalties": computation.employee_rows,
        "employee_recipients": computation.snapshot.get("employee_recipients", []),
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


def effective_total_shortage_considered(
    audit: InventoryAudit,
    fallback: Decimal,
) -> Decimal:
    snapshot = audit.computation_snapshot
    if not isinstance(snapshot, dict):
        return fallback
    groups = snapshot.get("groups")
    if not isinstance(groups, dict):
        return fallback
    total = Decimal("0")
    found = False
    for group in sorted(ALLOCATION_GROUPS):
        row = groups.get(group)
        if not isinstance(row, dict):
            continue
        total += Decimal(str(row.get("total_shortage") or row.get("sum") or "0"))
        found = True
    return total if found else fallback


def validate_allocation_group(value: str | None) -> None:
    if value is not None and value not in ALLOCATION_GROUPS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Некорректная группа распределения",
        )


async def validate_swap_group_allocation(
    session: AsyncSession,
    *,
    swap_group: str | None,
    allocation_group: str | None,
    current_position_id: uuid.UUID | None,
    current_position_name: str,
) -> None:
    if swap_group is None:
        return
    if allocation_group is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f'Группа пересорта "{swap_group}" содержит позицию без аллокации: '
                f"{current_position_name}. Приведите аллокацию к единой перед сохранением."
            ),
        )
    stmt = select(InventoryAuditPosition).where(
        InventoryAuditPosition.swap_group == swap_group,
    )
    if current_position_id is not None:
        stmt = stmt.where(InventoryAuditPosition.id != current_position_id)
    positions = (await session.scalars(stmt)).all()
    conflicts = [
        position
        for position in positions
        if getattr(position, "allocation_group", None) != allocation_group
    ]
    if not conflicts:
        return
    conflict = conflicts[0]
    detail = (
        f'Группа пересорта "{swap_group}" содержит позиции с разной аллокацией: \n'
        f"{current_position_name} ({allocation_group}), "
        f"{conflict.display_name} ({conflict.allocation_group}). \n"
        "Приведите аллокацию к единой перед сохранением."
    )
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def clean_swap_group_input(value: Any) -> str | None:
    text = clean_optional_text(value)
    if text is None:
        return None
    return text[:64]


def clean_swap_group_override(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return ""
    return text[:64]


def validate_audit_item_swap_override(audit: InventoryAudit, item: InventoryAuditItem) -> None:
    swap_group = item_effective_swap_group(item)
    if not swap_group:
        return
    allocation_group = item.position.allocation_group if item.position else None
    if allocation_group is None:
        return
    for candidate in audit.items:
        if candidate.id == item.id or item_effective_swap_group(candidate) != swap_group:
            continue
        candidate_group = candidate.position.allocation_group if candidate.position else None
        if candidate_group == allocation_group:
            continue
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f'Группа пересорта "{swap_group}" содержит позиции с разной аллокацией: \n'
                f"{item.product_name_snapshot} ({allocation_group}), "
                f"{candidate.product_name_snapshot} ({candidate_group}). \n"
                "Приведите аллокацию к единой перед сохранением."
            ),
        )


def clean_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

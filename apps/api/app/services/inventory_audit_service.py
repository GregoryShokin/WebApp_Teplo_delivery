from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentActor
from app.models import (
    AgentAction,
    AgentRun,
    AppSetting,
    Employee,
    InventoryAudit,
    InventoryAuditEmployeeExclusion,
    InventoryAuditItem,
    InventoryAuditItemExclusion,
    InventoryAuditPosition,
    PayrollAdjustment,
    PayrollAdjustmentCategory,
    ScheduledShift,
    ShiftLedgerEntry,
)
from app.services import iiko_inventory
from app.services.payroll_adjustment_service import (
    PayrollAdjustmentLockedError,
    assert_date_not_locked,
    is_date_locked,
)

ALLOCATION_GROUPS = {"chefs", "common", "admins"}
INVENTORY_ADJUSTMENT_CATEGORY_CODE = "inventory_shortage"
INVENTORY_ADJUSTMENT_COMMENT_PREFIX = "Недостача по ревизии"
SWAP_GROUP_MAX_LENGTH = 64
DEFAULT_SETTINGS = {
    "inventory.threshold_zero": Decimal("5000"),
    "inventory.threshold_50pct": Decimal("10000"),
    "inventory.rate_tier_40pct": Decimal("0.40"),
    "inventory.rate_tier_50pct": Decimal("0.50"),
}
MONEY = Decimal("0.01")


class InventoryAuditError(Exception):
    pass


class InventoryAuditNotFoundError(InventoryAuditError):
    pass


class InventoryAuditConflictError(InventoryAuditError):
    pass


class InventoryAuditValidationError(InventoryAuditError):
    pass


class IikoInventoryDocumentNotFoundError(InventoryAuditError):
    pass


@dataclass(slots=True)
class InventoryPositionsSyncResult:
    added: int
    updated: int
    total: int


@dataclass(slots=True)
class PenaltyComputation:
    audit_id: uuid.UUID
    total_shortage_amount: Decimal
    total_penalty_amount: Decimal
    period_start: date
    period_end: date
    groups: dict[str, dict[str, Any]]
    employee_penalties: dict[uuid.UUID, Decimal]
    employee_rows: list[dict[str, Any]]
    swap_groups: list[dict[str, Any]]
    warnings: list[str]
    snapshot: dict[str, Any]


@dataclass(slots=True)
class InventoryAuditItemsSummary:
    all_items: list[InventoryAuditItem]
    considered_items: list[InventoryAuditItem]
    total_shortage_iiko: Decimal
    total_shortage_considered: Decimal
    items_skipped_count: int


async def import_audit_from_iiko(
    session: AsyncSession,
    *,
    business_date: date,
    document_id: str | None = None,
    actor: CurrentActor,
) -> InventoryAudit:
    document = await _iiko_document_for_import(business_date, document_id)
    if document is None:
        raise IikoInventoryDocumentNotFoundError("Документ инвентаризации не найден")

    existing_audit = await session.scalar(
        select(InventoryAudit)
        .options(selectinload(InventoryAudit.items).selectinload(InventoryAuditItem.position))
        .where(InventoryAudit.business_date == business_date)
    )
    if existing_audit is not None and existing_audit.status != "draft":
        raise InventoryAuditConflictError("Ревизия за эту дату уже существует")
    positions = await _positions_by_guid(session)
    previous_audit_date = await _previous_audit_date(session, business_date)
    now = datetime.now(UTC)
    if existing_audit is None:
        audit = InventoryAudit(
            business_date=business_date,
            previous_audit_date=previous_audit_date,
            iiko_document_id=document.get("document_id"),
            iiko_document_num=document.get("document_num"),
            source="iiko",
            status="draft",
            total_shortage_amount=_money(document.get("total_shortage", Decimal("0"))),
            total_penalty_amount=Decimal("0"),
            created_by_user_id=actor.user_id,
            created_at=now,
            updated_at=now,
        )
        session.add(audit)
        await session.flush()
        action_type = "inventory_audit_import_iiko"
    else:
        audit = existing_audit
        for item in list(audit.items):
            await session.delete(item)
        await session.flush()
        audit.previous_audit_date = previous_audit_date
        audit.iiko_document_id = document.get("document_id")
        audit.iiko_document_num = document.get("document_num")
        audit.source = "iiko"
        audit.total_shortage_amount = _money(document.get("total_shortage", Decimal("0")))
        audit.total_penalty_amount = Decimal("0")
        audit.computation_snapshot = None
        audit.updated_at = now
        action_type = "inventory_audit_reimport_iiko"

    for raw_item in document.get("items", []):
        product_guid = raw_item.get("product_guid")
        position = positions.get(str(product_guid)) if product_guid else None
        shortage_amount = _money(raw_item.get("shortage_amount", Decimal("0")))
        signed_amount = _money(raw_item.get("amount", -shortage_amount))
        session.add(
            InventoryAuditItem(
                audit_id=audit.id,
                position_id=position.id if position is not None else None,
                iiko_product_guid=product_guid,
                product_name_snapshot=str(raw_item.get("product_name") or "Позиция iiko"),
                shortage_amount=shortage_amount,
                amount=signed_amount,
            )
        )

    await _write_agent_audit(
        session,
        action_type=action_type,
        audit=audit,
        actor=actor,
        after={"business_date": business_date.isoformat(), "document": document.get("document_id")},
    )
    await session.commit()
    return await _load_audit_or_404(session, audit.id)


async def list_iiko_candidates(
    session: AsyncSession,
    *,
    business_date: date,
) -> list[dict[str, Any]]:
    documents = await iiko_inventory.fetch_inventory_documents(business_date)
    active_positions = await _active_positions_by_guid(session)
    candidates: list[dict[str, Any]] = []
    for document in documents:
        matched_active_count = sum(
            1
            for raw_item in document.get("items", [])
            if raw_item.get("product_guid")
            and str(raw_item.get("product_guid")) in active_positions
        )
        candidates.append(
            {
                "document_id": str(document.get("document_id") or ""),
                "document_num": str(document.get("document_num") or ""),
                "items_count": len(document.get("items", [])),
                "total_shortage": decimal_string(document.get("total_shortage", Decimal("0"))),
                "matched_active_count": matched_active_count,
            }
        )
    return candidates


async def sync_positions_from_iiko(
    session: AsyncSession,
    *,
    actor: CurrentActor,
) -> InventoryPositionsSyncResult:
    products = await iiko_inventory.fetch_products_catalog(session, refresh=True)
    existing = (
        await session.scalars(
            select(InventoryAuditPosition).where(
                InventoryAuditPosition.iiko_product_guid.is_not(None)
            )
        )
    ).all()
    positions_by_guid = {str(position.iiko_product_guid): position for position in existing}
    added = 0
    updated = 0
    now = datetime.now(UTC)

    products_by_name = sorted(
        products.items(),
        key=lambda item: (item[1].get("name") or "", item[0]),
    )
    for guid, product in products_by_name:
        display_name = str(product.get("name") or "").strip()
        if not display_name:
            display_name = guid
        position = positions_by_guid.get(guid)
        if position is None:
            position = InventoryAuditPosition(
                code=f"iiko_{guid}"[:64],
                display_name=display_name,
                allocation_group=None,
                iiko_product_guid=guid,
                is_active=False,
                sort_order=100,
                created_at=now,
                updated_at=now,
            )
            session.add(position)
            added += 1
            continue
        position.display_name = display_name
        position.updated_at = now
        updated += 1

    result = InventoryPositionsSyncResult(
        added=added,
        updated=updated,
        total=len(products),
    )
    await _write_agent_audit(
        session,
        action_type="inventory_positions_sync",
        audit=None,
        actor=actor,
        after={"added": added, "updated": updated, "total": len(products)},
    )
    await session.commit()
    return result


async def create_manual_audit(
    session: AsyncSession,
    *,
    business_date: date,
    items: list[dict[str, Any]],
    actor: CurrentActor,
    notes: str | None = None,
) -> InventoryAudit:
    await _ensure_business_date_available(session, business_date)
    previous_audit_date = await _previous_audit_date(session, business_date)
    now = datetime.now(UTC)
    audit = InventoryAudit(
        business_date=business_date,
        previous_audit_date=previous_audit_date,
        source="manual",
        status="draft",
        total_shortage_amount=Decimal("0"),
        total_penalty_amount=Decimal("0"),
        notes=_clean_optional_text(notes),
        created_by_user_id=actor.user_id,
        created_at=now,
        updated_at=now,
    )
    session.add(audit)
    await session.flush()

    total = Decimal("0")
    for raw_item in items:
        position = await _position_from_payload(session, raw_item)
        name = raw_item.get("product_name_snapshot") or (
            position.display_name if position is not None else None
        )
        if not name:
            raise InventoryAuditValidationError("Укажите позицию или название товара")
        shortage_amount = _money(raw_item.get("shortage_amount", Decimal("0")))
        signed_amount = _money(raw_item.get("amount", -shortage_amount))
        total += shortage_amount
        session.add(
            InventoryAuditItem(
                audit_id=audit.id,
                position_id=position.id if position is not None else None,
                product_name_snapshot=str(name).strip(),
                shortage_amount=shortage_amount,
                amount=signed_amount,
            )
        )
    audit.total_shortage_amount = _money(total)
    audit.updated_at = datetime.now(UTC)
    await _write_agent_audit(
        session,
        action_type="inventory_audit_create_manual",
        audit=audit,
        actor=actor,
        after={"business_date": business_date.isoformat(), "item_count": len(items)},
    )
    await session.commit()
    return await _load_audit_or_404(session, audit.id)


async def compute_penalties(session: AsyncSession, audit_id: uuid.UUID) -> PenaltyComputation:
    computation = await _compute_penalties(session, audit_id, commit=True)
    return computation


async def get_audit_with_items(
    session: AsyncSession,
    audit_id: uuid.UUID,
) -> dict[str, Any]:
    audit = await _load_audit_or_404(session, audit_id)
    excluded_ids = {exclusion.item_id for exclusion in getattr(audit, "item_exclusions", [])}
    summary = summarize_audit_items(list(audit.items), excluded_ids=excluded_ids)
    return {
        "audit": audit,
        "items": summary.considered_items,
        "total_shortage_iiko": summary.total_shortage_iiko,
        "total_shortage_considered": summary.total_shortage_considered,
        "items_skipped_count": summary.items_skipped_count,
    }


async def list_all_exclusions(
    session: AsyncSession,
    *,
    audit_date_from: date | None = None,
    audit_date_to: date | None = None,
    employee_id: uuid.UUID | None = None,
) -> dict[str, list[dict[str, Any]]]:
    audit_filters = []
    if audit_date_from is not None:
        audit_filters.append(InventoryAudit.business_date >= audit_date_from)
    if audit_date_to is not None:
        audit_filters.append(InventoryAudit.business_date <= audit_date_to)

    item_q = (
        select(InventoryAuditItemExclusion)
        .options(
            selectinload(InventoryAuditItemExclusion.audit),
            selectinload(InventoryAuditItemExclusion.item),
            selectinload(InventoryAuditItemExclusion.created_by),
        )
        .join(InventoryAudit, InventoryAuditItemExclusion.audit_id == InventoryAudit.id)
        .order_by(
            InventoryAudit.business_date.desc(),
            InventoryAuditItemExclusion.created_at.desc(),
        )
    )
    for audit_filter in audit_filters:
        item_q = item_q.where(audit_filter)
    item_rows = (await session.scalars(item_q)).unique().all()

    emp_q = (
        select(InventoryAuditEmployeeExclusion)
        .options(
            selectinload(InventoryAuditEmployeeExclusion.audit),
            selectinload(InventoryAuditEmployeeExclusion.employee),
            selectinload(InventoryAuditEmployeeExclusion.created_by),
        )
        .join(InventoryAudit, InventoryAuditEmployeeExclusion.audit_id == InventoryAudit.id)
        .order_by(
            InventoryAudit.business_date.desc(),
            InventoryAuditEmployeeExclusion.created_at.desc(),
        )
    )
    for audit_filter in audit_filters:
        emp_q = emp_q.where(audit_filter)
    if employee_id is not None:
        emp_q = emp_q.where(InventoryAuditEmployeeExclusion.employee_id == employee_id)
    emp_rows = (await session.scalars(emp_q)).unique().all()

    return {
        "items": [
            {
                "id": str(exclusion.id),
                "audit_id": str(exclusion.audit_id),
                "audit_business_date": (
                    exclusion.audit.business_date if exclusion.audit is not None else None
                ),
                "audit_status": exclusion.audit.status if exclusion.audit is not None else None,
                "item_id": str(exclusion.item_id),
                "product_name": (
                    exclusion.item.product_name_snapshot if exclusion.item is not None else None
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
            }
            for exclusion in item_rows
        ],
        "employees": [
            {
                "id": str(exclusion.id),
                "audit_id": str(exclusion.audit_id),
                "audit_business_date": (
                    exclusion.audit.business_date if exclusion.audit is not None else None
                ),
                "audit_status": exclusion.audit.status if exclusion.audit is not None else None,
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
            }
            for exclusion in emp_rows
        ],
    }


def summarize_audit_items(
    items: list[InventoryAuditItem],
    *,
    excluded_ids: set[uuid.UUID] | None = None,
) -> InventoryAuditItemsSummary:
    all_items = list(items)
    considered_items = [
        item for item in all_items if audit_item_is_considered(item, excluded_ids=excluded_ids)
    ]
    total_shortage_iiko = _money(
        sum((item_shortage_amount(item) for item in all_items), Decimal("0"))
    )
    total_shortage_considered = _money(
        sum(
            (item_shortage_amount(item) for item in considered_items),
            Decimal("0"),
        )
    )
    return InventoryAuditItemsSummary(
        all_items=all_items,
        considered_items=considered_items,
        total_shortage_iiko=total_shortage_iiko,
        total_shortage_considered=total_shortage_considered,
        items_skipped_count=len(all_items) - len(considered_items),
    )


def audit_item_is_considered(
    item: Any,
    excluded_ids: set[uuid.UUID] | None = None,
) -> bool:
    item_id = getattr(item, "id", None)
    if item_id is not None and excluded_ids and item_id in excluded_ids:
        return False
    position = getattr(item, "position", None)
    return (
        position is not None
        and getattr(position, "is_active", False) is True
        and getattr(position, "allocation_group", None) is not None
    )


async def apply_audit_penalties(
    session: AsyncSession,
    audit_id: uuid.UUID,
    *,
    actor: CurrentActor,
) -> InventoryAudit:
    audit = await _load_audit_or_404(session, audit_id)
    if audit.status == "applied":
        raise InventoryAuditConflictError("Ревизия уже применена")
    if audit.status == "cancelled":
        raise InventoryAuditConflictError("Отменённую ревизию нельзя применить")
    adjustment_work_date = effective_penalty_work_date(audit)
    try:
        await assert_date_not_locked(session, adjustment_work_date)
    except PayrollAdjustmentLockedError as exc:
        raise InventoryAuditConflictError(str(exc)) from exc

    computation = await _compute_penalties(session, audit_id, commit=False)
    category = await _get_or_create_inventory_category(session)
    comment = adjustment_comment_for_computation(audit.business_date, computation)
    now = datetime.now(UTC)
    for employee_id, amount in computation.employee_penalties.items():
        if amount <= 0:
            continue
        session.add(
            PayrollAdjustment(
                employee_id=employee_id,
                work_date=adjustment_work_date,
                type="penalty",
                category_id=category.id,
                custom_label=None,
                amount=_money(amount),
                comment=comment,
                created_by_user_id=actor.user_id,
                created_by_label=_actor_label(actor),
                created_at=now,
                updated_at=now,
            )
        )

    audit.status = "applied"
    audit.applied_at = now
    audit.applied_by_user_id = actor.user_id
    audit.updated_at = now
    await _write_agent_audit(
        session,
        action_type="inventory_audit_apply",
        audit=audit,
        actor=actor,
        after={
            "total_penalty_amount": decimal_string(computation.total_penalty_amount),
            "employee_count": len(computation.employee_penalties),
        },
    )
    await session.commit()
    return await _load_audit_or_404(session, audit.id)


async def cancel_audit(
    session: AsyncSession,
    audit_id: uuid.UUID,
    *,
    actor: CurrentActor,
) -> InventoryAudit:
    audit = await _load_audit_or_404(session, audit_id)
    if audit.status == "cancelled":
        return audit

    removed_adjustments = 0
    if audit.status == "applied":
        adjustment_work_date = effective_penalty_work_date(audit)
        try:
            await assert_date_not_locked(session, adjustment_work_date)
        except PayrollAdjustmentLockedError as exc:
            raise InventoryAuditConflictError(str(exc)) from exc
        category = await _inventory_category(session)
        if category is not None:
            adjustments = (
                await session.scalars(
                    select(PayrollAdjustment).where(
                        PayrollAdjustment.type == "penalty",
                        PayrollAdjustment.category_id == category.id,
                        PayrollAdjustment.work_date == adjustment_work_date,
                        PayrollAdjustment.comment.like(
                            f"{adjustment_comment(audit.business_date)}%"
                        ),
                    )
                )
            ).all()
            for adjustment in adjustments:
                await session.delete(adjustment)
            removed_adjustments = len(adjustments)

    audit.status = "cancelled"
    audit.updated_at = datetime.now(UTC)
    await _write_agent_audit(
        session,
        action_type="inventory_audit_cancel",
        audit=audit,
        actor=actor,
        after={"removed_adjustments": removed_adjustments},
    )
    await session.commit()
    return await _load_audit_or_404(session, audit.id)


async def restore_cancelled_audit(
    session: AsyncSession,
    audit_id: uuid.UUID,
    *,
    actor: CurrentActor,
) -> InventoryAudit:
    audit = await _load_audit_or_404(session, audit_id)
    if audit.status != "cancelled":
        raise InventoryAuditConflictError("Вернуть в черновик можно только отменённую ревизию")

    now = datetime.now(UTC)
    audit.status = "draft"
    audit.applied_at = None
    audit.applied_by_user_id = None
    audit.updated_at = now
    await _write_agent_audit(
        session,
        action_type="inventory_audit_restore_draft",
        audit=audit,
        actor=actor,
        after={"from_status": "cancelled", "status": "draft"},
    )
    await session.commit()
    return await _load_audit_or_404(session, audit.id)


async def set_employee_exclusion(
    session: AsyncSession,
    audit_id: uuid.UUID,
    employee_id: uuid.UUID,
    *,
    excluded: bool,
    reason: str | None,
    actor: CurrentActor,
) -> InventoryAudit:
    audit = await _load_audit_or_404(session, audit_id)
    if audit.status != "draft":
        raise InventoryAuditConflictError("Исключать сотрудников можно только в черновике")

    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise InventoryAuditNotFoundError("Сотрудник не найден")

    existing = next(
        (row for row in audit.employee_exclusions if row.employee_id == employee_id),
        None,
    )
    before = (
        {
            "employee_id": str(employee_id),
            "full_name": employee.full_name,
            "reason": existing.reason,
        }
        if existing is not None
        else None
    )

    if excluded:
        clean_reason = _clean_optional_text(reason)
        if clean_reason is None:
            raise InventoryAuditValidationError("Укажите причину исключения сотрудника")
        clean_reason = clean_reason[:500]
        if existing is None:
            existing = InventoryAuditEmployeeExclusion(
                id=uuid.uuid4(),
                audit_id=audit.id,
                employee_id=employee_id,
                reason=clean_reason,
                created_by_user_id=actor.user_id,
            )
            session.add(existing)
        else:
            existing.reason = clean_reason
            existing.updated_at = datetime.now(UTC)
        after: dict[str, Any] = {
            "employee_id": str(employee_id),
            "full_name": employee.full_name,
            "excluded": True,
            "reason": clean_reason,
        }
    else:
        if existing is not None:
            await session.delete(existing)
            audit.employee_exclusions.remove(existing)
        after = {
            "employee_id": str(employee_id),
            "full_name": employee.full_name,
            "excluded": False,
        }

    await _write_agent_audit(
        session,
        action_type="inventory_employee_exclusion",
        audit=audit,
        actor=actor,
        before=before,
        after=after,
        target_table="inventory_audit_employee_exclusion",
        target_id=existing.id if existing is not None else employee_id,
    )
    await session.flush()
    await _compute_penalties(session, audit.id, commit=False)
    await session.commit()
    return await _load_audit_or_404(session, audit.id)


async def set_item_exclusion(
    session: AsyncSession,
    audit_id: uuid.UUID,
    item_id: uuid.UUID,
    *,
    excluded: bool,
    reason: str | None,
    actor: CurrentActor,
) -> InventoryAudit:
    audit = await _load_audit_or_404(session, audit_id)
    if audit.status != "draft":
        raise InventoryAuditConflictError("Исключать позиции можно только в черновике")

    item = next((row for row in audit.items if row.id == item_id), None)
    if item is None:
        raise InventoryAuditNotFoundError("Позиция не найдена")

    existing = next(
        (row for row in audit.item_exclusions if row.item_id == item_id),
        None,
    )
    item_label = getattr(item, "product_name_snapshot", "")
    before = (
        {
            "item_id": str(item_id),
            "product_name_snapshot": item_label,
            "reason": existing.reason,
        }
        if existing is not None
        else None
    )

    if excluded:
        clean_reason = _clean_optional_text(reason)
        if clean_reason is None:
            raise InventoryAuditValidationError("Укажите причину исключения позиции")
        clean_reason = clean_reason[:500]
        if existing is None:
            existing = InventoryAuditItemExclusion(
                id=uuid.uuid4(),
                audit_id=audit.id,
                item_id=item_id,
                reason=clean_reason,
                created_by_user_id=actor.user_id,
            )
            session.add(existing)
            audit.item_exclusions.append(existing)
        else:
            existing.reason = clean_reason
            existing.updated_at = datetime.now(UTC)
        after: dict[str, Any] = {
            "item_id": str(item_id),
            "product_name_snapshot": item_label,
            "excluded": True,
            "reason": clean_reason,
        }
    else:
        if existing is not None:
            await session.delete(existing)
            audit.item_exclusions.remove(existing)
        after = {
            "item_id": str(item_id),
            "product_name_snapshot": item_label,
            "excluded": False,
            "reason": None,
        }

    await _write_agent_audit(
        session,
        action_type="inventory_item_exclusion",
        audit=audit,
        actor=actor,
        before=before,
        after=after,
        target_table="inventory_audit_item_exclusion",
        target_id=existing.id if existing is not None else item_id,
    )
    await session.flush()
    await _compute_penalties(session, audit.id, commit=False)
    await session.commit()
    return await _load_audit_or_404(session, audit.id)


def build_penalty_computation(
    *,
    audit: Any,
    items: list[Any],
    settings: dict[str, Decimal] | None,
    chefs: list[Any],
    admins: list[Any],
    employee_exclusions: dict[uuid.UUID, str] | None = None,
    item_exclusions: dict[uuid.UUID, str] | None = None,
) -> PenaltyComputation:
    effective_settings = {**DEFAULT_SETTINGS, **(settings or {})}
    exclusions = employee_exclusions or {}
    included_chefs = _included_employees(chefs, exclusions)
    included_admins = _included_employees(admins, exclusions)
    period_start, period_end = audit_period(audit)
    groups = _group_shortages(items, effective_settings, item_exclusions=item_exclusions)
    skipped_items = groups.pop("_skipped_items", [])
    swap_groups = groups.pop("_swap_groups", [])
    employee_penalties: dict[uuid.UUID, Decimal] = defaultdict(lambda: Decimal("0"))
    warnings: list[str] = []

    _distribute_group(
        group_key="chefs",
        role_key="chefs",
        recipients=included_chefs,
        amount=_decimal(groups["chefs"]["penalty"]),
        employee_penalties=employee_penalties,
        groups=groups,
        warnings=warnings,
    )

    common_penalty = _decimal(groups["common"]["penalty"])
    common_half = _money(common_penalty / Decimal("2"))
    _distribute_group(
        group_key="common",
        role_key="chefs",
        recipients=included_chefs,
        amount=common_half,
        employee_penalties=employee_penalties,
        groups=groups,
        warnings=warnings,
    )
    _distribute_group(
        group_key="common",
        role_key="admins",
        recipients=included_admins,
        amount=common_penalty - common_half,
        employee_penalties=employee_penalties,
        groups=groups,
        warnings=warnings,
    )

    _distribute_group(
        group_key="admins",
        role_key="admins",
        recipients=included_admins,
        amount=_decimal(groups["admins"]["penalty"]),
        employee_penalties=employee_penalties,
        groups=groups,
        warnings=warnings,
    )

    employee_rows = [
        {
            "employee_id": str(employee_id),
            "full_name": _employee_name_by_id(employee_id, chefs, admins),
            "position": _employee_position_by_id(employee_id, chefs, admins),
            "amount": decimal_string(amount),
        }
        for employee_id, amount in sorted(
            employee_penalties.items(),
            key=lambda pair: _employee_name_by_id(pair[0], chefs, admins),
        )
        if amount > 0
    ]
    employee_recipient_rows = _employee_recipient_rows(
        chefs=chefs,
        admins=admins,
        employee_penalties=employee_penalties,
        exclusions=exclusions,
    )
    employee_penalties = {
        employee_id: _money(amount)
        for employee_id, amount in employee_penalties.items()
        if amount > 0
    }
    total_shortage = _money(
        sum(
            (
                _decimal(group["total_shortage"])
                for group_key, group in groups.items()
                if group_key in ALLOCATION_GROUPS
            ),
            Decimal("0"),
        )
    )
    total_penalty = _money(
        sum((_decimal(group["penalty"]) for group in groups.values()), Decimal("0"))
    )
    snapshot = {
        "period": {
            "start": period_start.isoformat(),
            "end": period_end.isoformat(),
            "previous_audit_date": getattr(audit, "previous_audit_date", None).isoformat()
            if getattr(audit, "previous_audit_date", None)
            else None,
        },
        "groups": groups,
        "employee_penalties": employee_rows,
        "employee_penalties_by_id": {
            str(employee_id): decimal_string(amount)
            for employee_id, amount in employee_penalties.items()
        },
        "employee_recipients": employee_recipient_rows,
        "skipped_items": skipped_items,
        "swap_groups": swap_groups,
        "warnings": warnings,
    }
    return PenaltyComputation(
        audit_id=audit.id,
        total_shortage_amount=total_shortage,
        total_penalty_amount=total_penalty,
        period_start=period_start,
        period_end=period_end,
        groups=groups,
        employee_penalties=employee_penalties,
        employee_rows=employee_rows,
        swap_groups=swap_groups,
        warnings=warnings,
        snapshot=snapshot,
    )


def lookup_rate(shortage_sum: Decimal, settings: dict[str, Decimal] | None = None) -> Decimal:
    rate, _reason = group_penalty_rate("chefs", shortage_sum, settings)
    return rate


def compute_group_penalty(
    group: str,
    total_shortage: Decimal,
    settings: dict[str, Decimal] | None,
) -> tuple[Decimal, Decimal]:
    """Возвращает ставку и сумму штрафа для группы распределения."""
    rate, _reason = group_penalty_rate(group, total_shortage, settings)
    penalty = _money(_decimal(total_shortage) * rate)
    return rate, penalty


def group_penalty_rate(
    group: str,
    total_shortage: Decimal,
    settings: dict[str, Decimal] | None = None,
) -> tuple[Decimal, str]:
    effective_settings = {**DEFAULT_SETTINGS, **(settings or {})}
    amount = _decimal(total_shortage)
    rate_50 = _decimal(effective_settings["inventory.rate_tier_50pct"])
    if group == "chefs":
        threshold_zero = _decimal(effective_settings["inventory.threshold_zero"])
        threshold_50 = _decimal(effective_settings["inventory.threshold_50pct"])
        rate_40 = _decimal(effective_settings["inventory.rate_tier_40pct"])
        if amount < threshold_zero:
            return Decimal("0"), "below_threshold"
        if amount < threshold_50:
            return rate_40, "tier_5k_10k"
        return rate_50, "tier_10k_plus"
    if group in {"common", "admins"}:
        return rate_50, "fixed_50pct"
    return Decimal("0"), "unknown_group"


def split_amount_evenly(amount: Decimal, employees: list[Any]) -> dict[uuid.UUID, Decimal]:
    amount = _money(amount)
    sorted_employees = sorted(employees, key=lambda employee: employee.full_name)
    if amount <= 0 or not sorted_employees:
        return {}
    share = _money(amount / Decimal(len(sorted_employees)))
    result = {employee.id: share for employee in sorted_employees}
    remainder = amount - sum(result.values(), Decimal("0"))
    first = sorted_employees[0]
    result[first.id] = _money(result[first.id] + remainder)
    return result


def audit_period(audit: Any) -> tuple[date, date]:
    previous = getattr(audit, "previous_audit_date", None)
    business_date = audit.business_date
    if previous is None:
        return business_date - timedelta(days=7), business_date
    return previous + timedelta(days=1), business_date


def adjustment_comment(business_date: date) -> str:
    return f"{INVENTORY_ADJUSTMENT_COMMENT_PREFIX} {business_date.isoformat()}"


def adjustment_comment_for_computation(
    business_date: date,
    computation: PenaltyComputation,
) -> str:
    lines = [adjustment_comment(business_date)]
    swap_groups = getattr(computation, "swap_groups", [])
    lines.extend(
        str(row["comment"])
        for row in swap_groups
        if _decimal(row.get("effective_shortage")) > 0 and row.get("comment")
    )
    return "\n".join(lines)


def audit_penalty_work_date(business_date: date) -> date:
    """Дата списания штрафа по умолчанию: день, следующий за ревизией.

    Для понедельничных ревизий это вторник — первый день следующего
    зарплатного периода (вторник…понедельник), поэтому штраф удерживается
    из выплаты примерно через неделю. Переопределяется на уровне ревизии
    полем ``penalty_work_date_override`` (см. :func:`effective_penalty_work_date`).
    """
    return business_date + timedelta(days=1)


def effective_penalty_work_date(audit: Any) -> date:
    """Фактическая дата списания: ручной перенос или правило по умолчанию."""
    override = getattr(audit, "penalty_work_date_override", None)
    if override is not None:
        return override
    return audit_penalty_work_date(audit.business_date)


def payroll_period_for_date(work_date: date) -> tuple[date, date, date]:
    """Зарплатный период (вторник…понедельник), содержащий дату, и дата выплаты.

    Возвращает ``(start, end, payout)``: start — вторник, end — понедельник,
    payout — следующий вторник (день выплаты этого периода).
    """
    offset = (work_date.weekday() - 1) % 7  # вторник=1 → 0
    start = work_date - timedelta(days=offset)
    end = start + timedelta(days=6)
    payout = end + timedelta(days=1)
    return start, end, payout


async def list_payout_options(
    session: AsyncSession,
    audit: Any,
    *,
    count: int = 8,
) -> list[dict[str, Any]]:
    """Список зарплатных выплат для переноса даты списания штрафа.

    Первый элемент — период по умолчанию (``override_value=None``), далее идут
    следующие недельные периоды. ``locked=True`` помечает уже зафиксированные
    (финализированные) периоды — переносить в них нельзя.
    """
    default_date = audit_penalty_work_date(audit.business_date)
    base_start, base_end, base_payout = payroll_period_for_date(default_date)
    options: list[dict[str, Any]] = []
    for index in range(max(count, 1)):
        shift = timedelta(days=7 * index)
        start = base_start + shift
        end = base_end + shift
        payout = base_payout + shift
        options.append(
            {
                "override_value": None if index == 0 else start,
                "period_start": start,
                "period_end": end,
                "payout_date": payout,
                "locked": await is_date_locked(session, start),
                "is_default": index == 0,
            }
        )
    return options


async def set_penalty_work_date_override(
    session: AsyncSession,
    audit: InventoryAudit,
    override: date | None,
    *,
    actor: CurrentActor,
) -> None:
    """Установить/сбросить перенос даты списания (только в черновике).

    Не коммитит — вызывающая сторона коммитит вместе с остальными правками.
    """
    if audit.status != "draft":
        raise InventoryAuditConflictError(
            "Перенести дату списания можно только в черновике"
        )
    if override is not None:
        default_date = audit_penalty_work_date(audit.business_date)
        if override < default_date:
            raise InventoryAuditValidationError(
                "Дата списания не может быть раньше расчётной (дата ревизии + 1 день)"
            )
        try:
            await assert_date_not_locked(session, override)
        except PayrollAdjustmentLockedError as exc:
            raise InventoryAuditConflictError(str(exc)) from exc
    before = audit.penalty_work_date_override
    audit.penalty_work_date_override = override
    audit.updated_at = datetime.now(UTC)
    await _write_agent_audit(
        session,
        action_type="inventory_audit_penalty_date_override",
        audit=audit,
        actor=actor,
        after={
            "before": before.isoformat() if before else None,
            "override": override.isoformat() if override else None,
        },
    )


async def _compute_penalties(
    session: AsyncSession,
    audit_id: uuid.UUID,
    *,
    commit: bool,
) -> PenaltyComputation:
    audit = await _load_audit_or_404(session, audit_id)
    settings = await _inventory_settings(session)
    period_start, period_end = audit_period(audit)
    chefs = await _load_period_employees(
        session,
        position="Повар",
        period_start=period_start,
        period_end=period_end,
    )
    admins = await _load_period_employees(
        session,
        position="Кассир",
        period_start=period_start,
        period_end=period_end,
    )
    computation = build_penalty_computation(
        audit=audit,
        items=audit.items,
        settings=settings,
        chefs=chefs,
        admins=admins,
        employee_exclusions={
            exclusion.employee_id: exclusion.reason
            for exclusion in getattr(audit, "employee_exclusions", [])
        },
        item_exclusions={
            exclusion.item_id: exclusion.reason
            for exclusion in getattr(audit, "item_exclusions", [])
        },
    )
    audit.total_shortage_amount = computation.total_shortage_amount
    audit.total_penalty_amount = computation.total_penalty_amount
    audit.computation_snapshot = computation.snapshot
    audit.updated_at = datetime.now(UTC)
    if commit:
        await session.commit()
    else:
        await session.flush()
    return computation


def _group_shortages(
    items: list[Any],
    settings: dict[str, Decimal],
    *,
    item_exclusions: dict[uuid.UUID, str] | None = None,
) -> dict[str, dict[str, Any]]:
    sums = {group: Decimal("0") for group in ALLOCATION_GROUPS}
    item_rows: dict[str, list[dict[str, Any]]] = {group: [] for group in ALLOCATION_GROUPS}
    swap_items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unmapped: list[dict[str, Any]] = []
    skipped_items: list[dict[str, Any]] = []
    for item in items:
        item_id = getattr(item, "id", None)
        if item_id is not None and item_exclusions and item_id in item_exclusions:
            skipped_items.append(
                {
                    "item_id": str(item_id),
                    "reason": "excluded",
                    "exclusion_reason": item_exclusions[item_id],
                }
            )
            continue
        position = getattr(item, "position", None)
        signed_amount = item_signed_amount(item)
        shortage_amount = item_shortage_amount(item)
        if position is None:
            skipped_items.append(
                {
                    "item_id": str(getattr(item, "id", "")),
                    "reason": "not_in_whitelist",
                }
            )
            unmapped.append(
                {
                    "item_id": str(getattr(item, "id", "")),
                    "product_name_snapshot": getattr(item, "product_name_snapshot", ""),
                    "shortage_amount": decimal_string(shortage_amount),
                    "amount": decimal_string(signed_amount),
                }
            )
            continue
        if not getattr(position, "is_active", True):
            skipped_items.append(
                {
                    "item_id": str(getattr(item, "id", "")),
                    "reason": "inactive",
                }
            )
            unmapped.append(
                {
                    "item_id": str(getattr(item, "id", "")),
                    "product_name_snapshot": getattr(item, "product_name_snapshot", ""),
                    "shortage_amount": decimal_string(shortage_amount),
                    "amount": decimal_string(signed_amount),
                }
            )
            continue
        group = getattr(position, "allocation_group", None)
        if group not in ALLOCATION_GROUPS:
            skipped_items.append(
                {
                    "item_id": str(getattr(item, "id", "")),
                    "reason": "no_group",
                }
            )
            continue
        effective_swap_group = item_effective_swap_group(item)
        row = {
            "item_id": str(getattr(item, "id", "")),
            "position_id": str(getattr(position, "id", "")),
            "position_code": getattr(position, "code", None),
            "display_name": getattr(position, "display_name", ""),
            "product_name_snapshot": getattr(item, "product_name_snapshot", ""),
            "allocation_group": group,
            "shortage_amount": decimal_string(shortage_amount),
            "amount": decimal_string(signed_amount),
            "swap_group": effective_swap_group,
            "swap_group_default": clean_swap_group(getattr(position, "swap_group", None)),
            "swap_group_override": item_swap_group_override(item),
            "has_swap_group_override": item_has_swap_group_override(item),
        }
        if effective_swap_group:
            swap_items[effective_swap_group].append(row)
            continue
        if shortage_amount <= 0:
            continue
        sums[group] += shortage_amount
        item_rows[group].append(
            {
                "item_id": row["item_id"],
                "position_id": row["position_id"],
                "position_code": row["position_code"],
                "display_name": row["display_name"],
                "shortage_amount": row["shortage_amount"],
                "amount": row["amount"],
            }
        )

    swap_groups: list[dict[str, Any]] = []
    for swap_group, rows in sorted(swap_items.items()):
        net_amount = _money(sum((_decimal(row["amount"]) for row in rows), Decimal("0")))
        allocation_groups = sorted({str(row["allocation_group"]) for row in rows})
        allocation_group = allocation_groups[0] if allocation_groups else None
        effective_shortage = _money(abs(net_amount)) if net_amount < 0 else Decimal("0")
        if allocation_group in ALLOCATION_GROUPS and effective_shortage > 0:
            sums[allocation_group] += effective_shortage
            item_rows[allocation_group].append(
                {
                    "type": "swap_group",
                    "swap_group": swap_group,
                    "display_name": f"Пересорт {swap_group}",
                    "shortage_amount": decimal_string(effective_shortage),
                    "amount": decimal_string(net_amount),
                    "net_amount": decimal_string(net_amount),
                    "items": rows,
                }
            )
        swap_groups.append(
            {
                "group": swap_group,
                "allocation_group": allocation_group,
                "allocation_groups": allocation_groups,
                "net_amount": decimal_string(net_amount),
                "effective_shortage": decimal_string(effective_shortage),
                "is_covered": net_amount >= 0,
                "items": rows,
                "comment": swap_group_comment_line(
                    swap_group,
                    rows,
                    net_amount,
                ),
            }
        )

    groups: dict[str, dict[str, Any]] = {}
    for group, shortage_sum in sorted(sums.items()):
        rate, penalty = compute_group_penalty(group, shortage_sum, settings)
        _rate, rate_reason = group_penalty_rate(group, shortage_sum, settings)
        groups[group] = {
            "group": group,
            "total_shortage": decimal_string(shortage_sum),
            "sum": decimal_string(shortage_sum),
            "rate": decimal_string(rate),
            "rate_reason": rate_reason,
            "rate_percent": decimal_string(rate * Decimal("100")),
            "penalty": decimal_string(penalty),
            "threshold": threshold_label(shortage_sum, settings, group=group),
            "items": item_rows[group],
            "recipients": {},
        }
    groups["unmapped"] = {
        "sum": decimal_string(
            sum((_decimal(row["shortage_amount"]) for row in unmapped), Decimal("0"))
        ),
        "rate": "0.00",
        "penalty": "0.00",
        "items": unmapped,
        "recipients": {},
    }
    groups["_skipped_items"] = skipped_items
    groups["_swap_groups"] = swap_groups
    return groups


def item_signed_amount(item: Any) -> Decimal:
    """Signed business amount: shortage < 0, surplus > 0."""
    if hasattr(item, "amount") and item.amount is not None:
        return _money(item.amount)
    shortage_amount = _money(getattr(item, "shortage_amount", Decimal("0")))
    if shortage_amount < 0:
        return shortage_amount
    return _money(-shortage_amount)


def item_shortage_amount(item: Any) -> Decimal:
    signed_amount = item_signed_amount(item)
    if signed_amount >= 0:
        return Decimal("0")
    return _money(abs(signed_amount))


def clean_swap_group(value: Any) -> str | None:
    text = _clean_optional_text(value)
    if text is None:
        return None
    return text[:SWAP_GROUP_MAX_LENGTH]


def item_has_swap_group_override(item: Any) -> bool:
    return getattr(item, "swap_group_override", None) is not None


def item_swap_group_override(item: Any) -> str | None:
    if not item_has_swap_group_override(item):
        return None
    value = getattr(item, "swap_group_override", None)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return ""
    return text[:SWAP_GROUP_MAX_LENGTH]


def item_effective_swap_group(item: Any) -> str | None:
    override = item_swap_group_override(item)
    if item_has_swap_group_override(item):
        return override or None
    position = getattr(item, "position", None)
    return clean_swap_group(getattr(position, "swap_group", None)) if position is not None else None


def summarize_swap_groups(items: list[Any]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        position = getattr(item, "position", None)
        allocation_group = getattr(position, "allocation_group", None)
        swap_group = item_effective_swap_group(item)
        if not swap_group or allocation_group not in ALLOCATION_GROUPS:
            continue
        grouped[swap_group].append(
            {
                "item_id": str(getattr(item, "id", "")),
                "position_id": str(getattr(position, "id", "")),
                "position_code": getattr(position, "code", None),
                "display_name": getattr(position, "display_name", ""),
                "product_name_snapshot": getattr(item, "product_name_snapshot", ""),
                "allocation_group": allocation_group,
                "amount": decimal_string(item_signed_amount(item)),
                "shortage_amount": decimal_string(item_shortage_amount(item)),
                "swap_group": swap_group,
                "swap_group_default": clean_swap_group(getattr(position, "swap_group", None)),
                "swap_group_override": item_swap_group_override(item),
                "has_swap_group_override": item_has_swap_group_override(item),
            }
        )

    summaries: list[dict[str, Any]] = []
    for swap_group, rows in sorted(grouped.items()):
        net_amount = _money(sum((_decimal(row["amount"]) for row in rows), Decimal("0")))
        allocation_groups = sorted({str(row["allocation_group"]) for row in rows})
        effective_shortage = _money(abs(net_amount)) if net_amount < 0 else Decimal("0")
        summaries.append(
            {
                "group": swap_group,
                "allocation_group": allocation_groups[0] if allocation_groups else None,
                "allocation_groups": allocation_groups,
                "net_amount": decimal_string(net_amount),
                "effective_shortage": decimal_string(effective_shortage),
                "is_covered": net_amount >= 0,
                "items": rows,
                "comment": swap_group_comment_line(swap_group, rows, net_amount),
            }
        )
    return summaries


def swap_group_comment_line(
    swap_group: str,
    rows: list[dict[str, Any]],
    net_amount: Decimal,
) -> str:
    item_details = ", ".join(
        f"{row.get('product_name_snapshot') or row.get('display_name') or 'Позиция'} "
        f"{money_comment_amount(_decimal(row.get('amount')))}"
        for row in rows
    )
    return f"Пересорт {swap_group}: {item_details}, итог {money_comment_amount(net_amount)}"


def money_comment_amount(value: Any) -> str:
    amount = _money(value)
    sign = "+" if amount > 0 else ""
    formatted = f"{abs(amount):,.0f}".replace(",", " ")
    if amount < 0:
        return f"-{formatted} ₽"
    return f"{sign}{formatted} ₽"


def threshold_label(
    shortage_sum: Decimal,
    settings: dict[str, Decimal] | None = None,
    *,
    group: str = "chefs",
) -> str:
    _rate, reason = group_penalty_rate(group, shortage_sum, settings)
    return {
        "below_threshold": "<5K",
        "tier_5k_10k": "5-10K",
        "tier_10k_plus": ">=10K",
        "fixed_50pct": "50% всегда",
    }.get(reason, "—")


def _distribute_group(
    *,
    group_key: str,
    role_key: str,
    recipients: list[Any],
    amount: Decimal,
    employee_penalties: dict[uuid.UUID, Decimal],
    groups: dict[str, dict[str, Any]],
    warnings: list[str],
) -> None:
    if amount <= 0:
        return
    if not recipients:
        warnings.append(f"Нет сотрудников для распределения группы {group_key}:{role_key}")
        return
    shares = split_amount_evenly(amount, recipients)
    groups[group_key]["recipients"][role_key] = {
        "total": decimal_string(amount),
        "count": len(recipients),
        "items": [
            {
                "employee_id": str(employee.id),
                "full_name": employee.full_name,
                "position": employee.position,
                "amount": decimal_string(shares.get(employee.id, Decimal("0"))),
            }
            for employee in sorted(recipients, key=lambda item: item.full_name)
        ],
    }
    for employee in sorted(recipients, key=lambda item: item.full_name):
        share = shares.get(employee.id, Decimal("0"))
        if share <= 0:
            continue
        employee_penalties[employee.id] += share


def _included_employees(
    employees: list[Any],
    exclusions: dict[uuid.UUID, str],
) -> list[Any]:
    return [employee for employee in employees if employee.id not in exclusions]


def _employee_recipient_rows(
    *,
    chefs: list[Any],
    admins: list[Any],
    employee_penalties: dict[uuid.UUID, Decimal],
    exclusions: dict[uuid.UUID, str],
) -> list[dict[str, Any]]:
    rows: dict[uuid.UUID, dict[str, Any]] = {}
    for recipient_group, employees in (("chefs", chefs), ("admins", admins)):
        for employee in employees:
            rows[employee.id] = {
                "employee_id": str(employee.id),
                "full_name": employee.full_name,
                "position": employee.position,
                "recipient_group": recipient_group,
                "amount": decimal_string(employee_penalties.get(employee.id, Decimal("0"))),
                "is_excluded": employee.id in exclusions,
                "exclusion_reason": exclusions.get(employee.id),
            }
    return sorted(rows.values(), key=lambda row: str(row["full_name"]))


async def _load_audit_or_404(session: AsyncSession, audit_id: uuid.UUID) -> InventoryAudit:
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
        raise InventoryAuditNotFoundError("Ревизия не найдена")
    return audit


async def _ensure_business_date_available(session: AsyncSession, business_date: date) -> None:
    existing = await session.scalar(
        select(InventoryAudit.id).where(InventoryAudit.business_date == business_date).limit(1)
    )
    if existing is not None:
        raise InventoryAuditConflictError("Ревизия за эту дату уже существует")


async def _previous_audit_date(session: AsyncSession, business_date: date) -> date | None:
    return await session.scalar(
        select(InventoryAudit.business_date)
        .where(
            InventoryAudit.business_date < business_date,
            InventoryAudit.status != "cancelled",
        )
        .order_by(InventoryAudit.business_date.desc())
        .limit(1)
    )


async def _active_positions_by_guid(
    session: AsyncSession,
) -> dict[str, InventoryAuditPosition]:
    positions = (
        await session.scalars(
            select(InventoryAuditPosition).where(
                InventoryAuditPosition.is_active.is_(True),
                InventoryAuditPosition.allocation_group.is_not(None),
                InventoryAuditPosition.iiko_product_guid.is_not(None),
            )
        )
    ).all()
    return {str(position.iiko_product_guid): position for position in positions}


async def _positions_by_guid(
    session: AsyncSession,
) -> dict[str, InventoryAuditPosition]:
    positions = (
        await session.scalars(
            select(InventoryAuditPosition).where(
                InventoryAuditPosition.iiko_product_guid.is_not(None),
            )
        )
    ).all()
    return {str(position.iiko_product_guid): position for position in positions}


async def _position_from_payload(
    session: AsyncSession,
    payload: dict[str, Any],
) -> InventoryAuditPosition | None:
    position_id = payload.get("position_id")
    position_code = payload.get("position_code")
    if position_id:
        position = await session.get(InventoryAuditPosition, position_id)
    elif position_code:
        position = await session.scalar(
            select(InventoryAuditPosition).where(InventoryAuditPosition.code == position_code)
        )
    else:
        return None
    if position is None:
        raise InventoryAuditValidationError("Позиция whitelist не найдена")
    return position


async def _inventory_settings(session: AsyncSession) -> dict[str, Decimal]:
    rows = (
        await session.execute(
            select(AppSetting.key, AppSetting.value).where(
                AppSetting.key.in_(tuple(DEFAULT_SETTINGS))
            )
        )
    ).all()
    values = DEFAULT_SETTINGS.copy()
    for key, raw_value in rows:
        values[key] = _decimal(raw_value)
    return values


async def _load_period_employees(
    session: AsyncSession,
    *,
    position: str,
    period_start: date,
    period_end: date,
) -> list[Employee]:
    ledger_exists = (
        select(ShiftLedgerEntry.id)
        .where(
            ShiftLedgerEntry.employee_id == Employee.id,
            ShiftLedgerEntry.work_date >= period_start,
            ShiftLedgerEntry.work_date <= period_end,
        )
        .exists()
    )
    scheduled_exists = (
        select(ScheduledShift.id)
        .where(
            ScheduledShift.employee_id == Employee.id,
            ScheduledShift.business_date >= period_start,
            ScheduledShift.business_date <= period_end,
        )
        .exists()
    )
    return (
        await session.scalars(
            select(Employee)
            .where(
                Employee.position == position,
                Employee.status == "active",
                or_(ledger_exists, scheduled_exists),
            )
            .order_by(Employee.full_name)
        )
    ).all()


async def _get_or_create_inventory_category(
    session: AsyncSession,
) -> PayrollAdjustmentCategory:
    category = await _inventory_category(session)
    if category is not None:
        return category
    category = PayrollAdjustmentCategory(
        type="penalty",
        code=INVENTORY_ADJUSTMENT_CATEGORY_CODE,
        display_name="Недостача по ревизии",
        sort_order=50,
        is_active=True,
    )
    session.add(category)
    await session.flush()
    return category


async def _inventory_category(session: AsyncSession) -> PayrollAdjustmentCategory | None:
    return await session.scalar(
        select(PayrollAdjustmentCategory).where(
            PayrollAdjustmentCategory.code == INVENTORY_ADJUSTMENT_CATEGORY_CODE
        )
    )


async def _write_agent_audit(
    session: AsyncSession,
    *,
    action_type: str,
    audit: InventoryAudit | None,
    actor: CurrentActor,
    after: dict[str, Any],
    before: dict[str, Any] | None = None,
    target_table: str | None = None,
    target_id: uuid.UUID | None = None,
) -> None:
    now = datetime.now(UTC)
    run = AgentRun(
        agent_name="inventory_audit",
        started_at=now,
        finished_at=now,
        status="success",
        params={
            "actor_roles": sorted(actor.roles),
            "actor_user_id": str(actor.user_id) if actor.user_id else None,
        },
        result=after,
    )
    session.add(run)
    await session.flush()
    session.add(
        AgentAction(
            agent_run_id=run.id,
            action_type=action_type,
            target_table=target_table
            or ("inventory_audit" if audit is not None else "inventory_audit_position"),
            target_id=target_id
            if target_id is not None
            else (audit.id if audit is not None else None),
            before_value=before,
            after_value=after,
        )
    )


async def write_swap_override_audit(
    session: AsyncSession,
    *,
    audit: InventoryAudit,
    item: InventoryAuditItem,
    actor: CurrentActor,
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    await _write_agent_audit(
        session,
        action_type="inventory_swap_override",
        audit=audit,
        actor=actor,
        before=before,
        after=after,
        target_table="inventory_audit_item",
        target_id=item.id,
    )


async def _iiko_document_for_import(
    business_date: date,
    document_id: str | None,
) -> dict[str, Any] | None:
    if document_id is None:
        return await iiko_inventory.fetch_inventory_document(business_date)
    documents = await iiko_inventory.fetch_inventory_documents(business_date)
    for document in documents:
        if str(document.get("document_id") or "") == document_id:
            return document
    return None


def decimal_string(value: Any) -> str:
    return f"{_money(value)}"


def _money(value: Any) -> Decimal:
    return _decimal(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _clean_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _actor_label(actor: CurrentActor) -> str | None:
    return ", ".join(sorted(actor.roles)) or None


def _employee_name_by_id(employee_id: uuid.UUID, *groups: list[Any]) -> str:
    for group in groups:
        for employee in group:
            if employee.id == employee_id:
                return employee.full_name
    return str(employee_id)


def _employee_position_by_id(employee_id: uuid.UUID, *groups: list[Any]) -> str | None:
    for group in groups:
        for employee in group:
            if employee.id == employee_id:
                return employee.position
    return None

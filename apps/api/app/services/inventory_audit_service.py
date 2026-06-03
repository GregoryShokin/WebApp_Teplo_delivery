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
    InventoryAuditItem,
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
)

ALLOCATION_GROUPS = {"chefs", "common", "admins"}
INVENTORY_ADJUSTMENT_CATEGORY_CODE = "inventory_shortage"
INVENTORY_ADJUSTMENT_COMMENT_PREFIX = "Недостача по ревизии"
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

    await _ensure_business_date_available(session, business_date)
    positions = await _positions_by_guid(session)
    previous_audit_date = await _previous_audit_date(session, business_date)
    now = datetime.now(UTC)
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

    for raw_item in document.get("items", []):
        product_guid = raw_item.get("product_guid")
        position = positions.get(str(product_guid)) if product_guid else None
        session.add(
            InventoryAuditItem(
                audit_id=audit.id,
                position_id=position.id if position is not None else None,
                iiko_product_guid=product_guid,
                product_name_snapshot=str(raw_item.get("product_name") or "Позиция iiko"),
                shortage_amount=_money(raw_item.get("shortage_amount", Decimal("0"))),
            )
        )

    await _write_agent_audit(
        session,
        action_type="inventory_audit_import_iiko",
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
        amount = _money(raw_item.get("shortage_amount", Decimal("0")))
        total += amount
        session.add(
            InventoryAuditItem(
                audit_id=audit.id,
                position_id=position.id if position is not None else None,
                product_name_snapshot=str(name).strip(),
                shortage_amount=amount,
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
    summary = summarize_audit_items(list(audit.items))
    return {
        "audit": audit,
        "items": summary.considered_items,
        "total_shortage_iiko": summary.total_shortage_iiko,
        "total_shortage_considered": summary.total_shortage_considered,
        "items_skipped_count": summary.items_skipped_count,
    }


def summarize_audit_items(items: list[InventoryAuditItem]) -> InventoryAuditItemsSummary:
    all_items = list(items)
    considered_items = [item for item in all_items if audit_item_is_considered(item)]
    total_shortage_iiko = _money(
        sum((_decimal(getattr(item, "shortage_amount", 0)) for item in all_items), Decimal("0"))
    )
    total_shortage_considered = _money(
        sum(
            (_decimal(getattr(item, "shortage_amount", 0)) for item in considered_items),
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


def audit_item_is_considered(item: Any) -> bool:
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
    adjustment_work_date = audit_penalty_work_date(audit.business_date)
    try:
        await assert_date_not_locked(session, adjustment_work_date)
    except PayrollAdjustmentLockedError as exc:
        raise InventoryAuditConflictError(str(exc)) from exc

    computation = await _compute_penalties(session, audit_id, commit=False)
    category = await _get_or_create_inventory_category(session)
    comment = adjustment_comment(audit.business_date)
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
        adjustment_work_date = audit_penalty_work_date(audit.business_date)
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
                        PayrollAdjustment.comment == adjustment_comment(audit.business_date),
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


def build_penalty_computation(
    *,
    audit: Any,
    items: list[Any],
    settings: dict[str, Decimal] | None,
    chefs: list[Any],
    admins: list[Any],
) -> PenaltyComputation:
    effective_settings = {**DEFAULT_SETTINGS, **(settings or {})}
    period_start, period_end = audit_period(audit)
    groups = _group_shortages(items, effective_settings)
    skipped_items = groups.pop("_skipped_items", [])
    employee_penalties: dict[uuid.UUID, Decimal] = defaultdict(lambda: Decimal("0"))
    warnings: list[str] = []

    _distribute_group(
        group_key="chefs",
        role_key="chefs",
        recipients=chefs,
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
        recipients=chefs,
        amount=common_half,
        employee_penalties=employee_penalties,
        groups=groups,
        warnings=warnings,
    )
    _distribute_group(
        group_key="common",
        role_key="admins",
        recipients=admins,
        amount=common_penalty - common_half,
        employee_penalties=employee_penalties,
        groups=groups,
        warnings=warnings,
    )

    _distribute_group(
        group_key="admins",
        role_key="admins",
        recipients=admins,
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
    employee_penalties = {
        employee_id: _money(amount)
        for employee_id, amount in employee_penalties.items()
        if amount > 0
    }
    total_shortage = _money(
        sum((_decimal(getattr(item, "shortage_amount", 0)) for item in items), Decimal("0"))
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
        "skipped_items": skipped_items,
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


def audit_penalty_work_date(business_date: date) -> date:
    return business_date + timedelta(days=1)


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
) -> dict[str, dict[str, Any]]:
    sums = {group: Decimal("0") for group in ALLOCATION_GROUPS}
    item_rows: dict[str, list[dict[str, Any]]] = {group: [] for group in ALLOCATION_GROUPS}
    unmapped: list[dict[str, Any]] = []
    skipped_items: list[dict[str, Any]] = []
    for item in items:
        position = getattr(item, "position", None)
        amount = _money(getattr(item, "shortage_amount", Decimal("0")))
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
                    "shortage_amount": decimal_string(amount),
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
                    "shortage_amount": decimal_string(amount),
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
        sums[group] += amount
        item_rows[group].append(
            {
                "item_id": str(getattr(item, "id", "")),
                "position_id": str(getattr(position, "id", "")),
                "position_code": getattr(position, "code", None),
                "display_name": getattr(position, "display_name", ""),
                "shortage_amount": decimal_string(amount),
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
    return groups


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


async def _load_audit_or_404(session: AsyncSession, audit_id: uuid.UUID) -> InventoryAudit:
    audit = await session.scalar(
        select(InventoryAudit)
        .options(selectinload(InventoryAudit.items).selectinload(InventoryAuditItem.position))
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
            target_table="inventory_audit" if audit is not None else "inventory_audit_position",
            target_id=audit.id if audit is not None else None,
            before_value=None,
            after_value=after,
        )
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

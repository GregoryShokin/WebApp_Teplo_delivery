from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentActor
from app.models import (
    DeferredAuditCharge,
    DeferredAuditChargeSplit,
    Employee,
    InventoryAudit,
    InventoryAuditItem,
    PayrollAdjustment,
    PayrollAdjustmentCategory,
    PayrollRun,
)
from app.schemas.payroll import DeferredChargeCreate

DEFERRED_AUDIT_CATEGORY_CODE = "audit_deferred"
DEFERRED_AUDIT_CATEGORY_NAME = "Распределённый штраф ревизии"
DEFERRED_AUDIT_CREATED_BY_LABEL = "system:deferred_charge"
PENDING_STATUSES = ("pending", "partially_applied")
MONEY = Decimal("0.01")


class DeferredChargeNotFoundError(LookupError):
    pass


class DeferredChargeValidationError(ValueError):
    pass


class DeferredChargeConflictError(RuntimeError):
    pass


async def create_deferred_charge(
    session: AsyncSession,
    payload: DeferredChargeCreate,
    actor: CurrentActor,
) -> DeferredAuditCharge:
    audit = await session.get(InventoryAudit, payload.source_audit_id)
    if audit is None:
        raise DeferredChargeNotFoundError("Ревизия не найдена")

    if payload.source_item_id is not None:
        item = await session.get(InventoryAuditItem, payload.source_item_id)
        if item is None:
            raise DeferredChargeNotFoundError("Позиция ревизии не найдена")
        if item.audit_id != payload.source_audit_id:
            raise DeferredChargeValidationError("Позиция не относится к выбранной ревизии")

    employee = await session.get(Employee, payload.employee_id)
    if employee is None:
        raise DeferredChargeNotFoundError("Сотрудник не найден")

    amounts = split_charge_amount(payload.total_amount, payload.splits_count)
    charge = DeferredAuditCharge(
        source_audit_id=payload.source_audit_id,
        source_item_id=payload.source_item_id,
        employee_id=payload.employee_id,
        total_amount=sum(amounts, Decimal("0")),
        splits_count=payload.splits_count,
        splits_remaining=payload.splits_count,
        status="pending",
        reason=payload.reason.strip(),
        applied_run_ids=[],
        created_by_user_id=actor.user_id,
    )
    charge.splits = [
        DeferredAuditChargeSplit(split_index=index, amount=amount)
        for index, amount in enumerate(amounts, start=1)
    ]
    session.add(charge)
    await session.flush()
    await session.commit()
    await session.refresh(charge)
    return await _load_deferred_charge(session, charge.id)


async def list_deferred_charges(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID | None = None,
    status: str | None = None,
    audit_id: uuid.UUID | None = None,
) -> list[DeferredAuditCharge]:
    query = select(DeferredAuditCharge).options(*_charge_load_options())
    if employee_id is not None:
        query = query.where(DeferredAuditCharge.employee_id == employee_id)
    if status is not None:
        query = query.where(DeferredAuditCharge.status == status)
    if audit_id is not None:
        query = query.where(DeferredAuditCharge.source_audit_id == audit_id)
    result = await session.scalars(
        query.order_by(DeferredAuditCharge.created_at.desc(), DeferredAuditCharge.id.desc())
    )
    return list(result.all())


async def cancel_deferred_charge(
    session: AsyncSession,
    charge_id: uuid.UUID,
    actor: CurrentActor,
) -> DeferredAuditCharge:
    charge = await _load_deferred_charge_for_update(session, charge_id)
    if charge.status not in PENDING_STATUSES:
        raise DeferredChargeConflictError("Отложенный штраф уже завершён")

    now = datetime.now(UTC)
    for split in charge.splits:
        if split.run_id is None:
            split.applied_at = split.applied_at or now
            split.adjustment_id = None
    charge.status = "cancelled"
    charge.splits_remaining = 0
    charge.updated_at = now

    await session.flush()
    await session.commit()
    await session.refresh(charge)
    return await _load_deferred_charge(session, charge.id)


async def apply_pending_splits_for_run(
    session: AsyncSession,
    run: PayrollRun,
    period_end: date,
) -> list[DeferredAuditChargeSplit]:
    result = await session.scalars(
        select(DeferredAuditCharge)
        .options(
            selectinload(DeferredAuditCharge.splits),
            selectinload(DeferredAuditCharge.source_audit),
        )
        .where(DeferredAuditCharge.status.in_(PENDING_STATUSES))
        .order_by(DeferredAuditCharge.created_at, DeferredAuditCharge.id)
        .with_for_update(skip_locked=True)
    )
    charges = list(result.all())
    applied: list[DeferredAuditChargeSplit] = []
    category: PayrollAdjustmentCategory | None = None
    now = datetime.now(UTC)
    run_id = str(run.id)

    for charge in charges:
        if run_id in (charge.applied_run_ids or []):
            continue
        if any(split.run_id == run.id for split in charge.splits):
            continue

        split = first_unapplied_split(charge)
        if split is None:
            if charge.splits_remaining != 0:
                charge.splits_remaining = 0
                charge.status = "applied"
                charge.updated_at = now
            continue
        if split.amount <= 0:
            raise DeferredChargeValidationError("Доля отложенного штрафа должна быть больше 0")

        if category is None:
            category = await _get_or_create_deferred_category(session)
        adjustment = PayrollAdjustment(
            id=uuid.uuid4(),
            employee_id=charge.employee_id,
            work_date=period_end,
            type="penalty",
            category_id=category.id,
            custom_label=None,
            amount=split.amount,
            comment=deferred_adjustment_comment(charge, split),
            created_by_user_id=None,
            created_by_label=DEFERRED_AUDIT_CREATED_BY_LABEL,
            created_at=now,
            updated_at=now,
        )
        session.add(adjustment)
        split.run_id = run.id
        split.adjustment_id = adjustment.id
        split.applied_at = now
        charge.applied_run_ids = [*(charge.applied_run_ids or []), run_id]
        charge.splits_remaining = max(0, charge.splits_remaining - 1)
        charge.status = "applied" if charge.splits_remaining == 0 else "partially_applied"
        charge.updated_at = now
        applied.append(split)

    if applied:
        await session.flush()
    return applied


def split_charge_amount(total_amount: Decimal, splits_count: int) -> list[Decimal]:
    total = total_amount.quantize(MONEY, rounding=ROUND_HALF_UP)
    base = (total / Decimal(splits_count)).quantize(MONEY, rounding=ROUND_HALF_UP)
    amounts = [base for _index in range(max(0, splits_count - 1))]
    amounts.append((total - base * Decimal(max(0, splits_count - 1))).quantize(MONEY))
    if any(amount <= 0 for amount in amounts):
        raise DeferredChargeValidationError("Каждая доля отложенного штрафа должна быть больше 0")
    return amounts


def first_unapplied_split(charge: DeferredAuditCharge) -> DeferredAuditChargeSplit | None:
    return next(
        (
            split
            for split in sorted(charge.splits, key=lambda item: item.split_index)
            if split.run_id is None and split.applied_at is None
        ),
        None,
    )


def deferred_adjustment_comment(
    charge: DeferredAuditCharge,
    split: DeferredAuditChargeSplit,
) -> str:
    return (
        f"Распределённый штраф ревизии {source_audit_date_string(charge)}, "
        f"доля {split.split_index}/{charge.splits_count}: {charge.reason[:200]}"
    )


def source_audit_date_string(charge: DeferredAuditCharge) -> str:
    audit_date = getattr(getattr(charge, "source_audit", None), "business_date", None)
    if isinstance(audit_date, date):
        return audit_date.isoformat()
    return str(charge.source_audit_id)


async def _get_or_create_deferred_category(
    session: AsyncSession,
) -> PayrollAdjustmentCategory:
    category = await session.scalar(
        select(PayrollAdjustmentCategory).where(
            PayrollAdjustmentCategory.code == DEFERRED_AUDIT_CATEGORY_CODE
        )
    )
    if category is not None:
        return category
    category = PayrollAdjustmentCategory(
        type="penalty",
        code=DEFERRED_AUDIT_CATEGORY_CODE,
        display_name=DEFERRED_AUDIT_CATEGORY_NAME,
        sort_order=55,
        is_active=True,
    )
    session.add(category)
    await session.flush()
    return category


async def _load_deferred_charge(
    session: AsyncSession,
    charge_id: uuid.UUID,
) -> DeferredAuditCharge:
    charge = await session.scalar(
        select(DeferredAuditCharge)
        .options(*_charge_load_options())
        .where(DeferredAuditCharge.id == charge_id)
    )
    if charge is None:
        raise DeferredChargeNotFoundError("Отложенный штраф не найден")
    return charge


async def _load_deferred_charge_for_update(
    session: AsyncSession,
    charge_id: uuid.UUID,
) -> DeferredAuditCharge:
    charge = await session.scalar(
        select(DeferredAuditCharge)
        .options(*_charge_load_options())
        .where(DeferredAuditCharge.id == charge_id)
        .with_for_update()
    )
    if charge is None:
        raise DeferredChargeNotFoundError("Отложенный штраф не найден")
    return charge


def _charge_load_options() -> tuple[Any, ...]:
    return (
        selectinload(DeferredAuditCharge.splits),
        selectinload(DeferredAuditCharge.source_audit),
        selectinload(DeferredAuditCharge.source_item),
        selectinload(DeferredAuditCharge.employee),
        selectinload(DeferredAuditCharge.created_by),
    )

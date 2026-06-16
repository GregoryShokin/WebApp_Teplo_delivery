from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentActor
from app.models import (
    DeferredAuditCharge,
    DeferredAuditChargeRecipient,
    DeferredAuditChargeSplit,
    Employee,
    InventoryAudit,
    InventoryAuditItem,
    PayrollAdjustment,
    PayrollAdjustmentCategory,
    PayrollLine,
    PayrollRun,
)
from app.schemas.payroll import DeferredChargeCreate
from app.services.inventory_audit_service import (
    _load_period_employees,
    audit_period,
    is_date_locked,
    payroll_period_for_date,
    split_amount_evenly,
)

DEFERRED_AUDIT_CATEGORY_CODE = "audit_deferred"
DEFERRED_AUDIT_CATEGORY_NAME = "Распределённый штраф ревизии"
DEFERRED_AUDIT_CREATED_BY_LABEL = "system:deferred_charge"
DEFERRED_AUDIT_DISMISSAL_CREATED_BY_LABEL = "system:deferred_charge_dismissal"
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
    audit = await _load_audit_with_exclusions(session, payload.source_audit_id)
    item = await _load_audit_item_with_position(session, payload.source_item_id)
    if item.audit_id != payload.source_audit_id:
        raise DeferredChargeValidationError("Позиция не относится к выбранной ревизии")

    position = getattr(item, "position", None)
    allocation_group = getattr(position, "allocation_group", None)
    if position is None or allocation_group is None:
        raise DeferredChargeValidationError("У позиции нет allocation_group")
    if allocation_group not in {"chefs", "admins", "common"}:
        raise DeferredChargeValidationError("Неподдерживаемая группа распределения")

    recipients, recipient_shares = await _recipient_shares_for_charge(
        session,
        audit=audit,
        allocation_group=allocation_group,
        total_penalty_amount=payload.total_penalty_amount,
    )
    if not recipients:
        raise DeferredChargeValidationError("Нет получателей штрафа за этот период")

    start_period_start: date | None = None
    if payload.start_period_start is not None:
        # Нормализуем на начало зарплатного периода (вторник) и запрещаем
        # старт в уже зафиксированной выплате.
        start_period_start = payroll_period_for_date(payload.start_period_start)[0]
        if await is_date_locked(session, start_period_start):
            raise DeferredChargeValidationError(
                "Стартовая выплата уже зафиксирована — выберите открытую"
            )

    charge = DeferredAuditCharge(
        source_audit_id=payload.source_audit_id,
        source_item_id=payload.source_item_id,
        allocation_group=allocation_group,
        total_penalty_amount=_money(payload.total_penalty_amount),
        splits_count=payload.splits_count,
        start_period_start=start_period_start,
        status="pending",
        reason=payload.reason.strip(),
        created_by_user_id=actor.user_id,
    )
    charge.recipients = [
        _build_recipient(employee, recipient_shares[employee.id], payload.splits_count)
        for employee in recipients
    ]
    session.add(charge)
    await session.flush()
    await session.commit()
    await session.refresh(charge)
    return await _load_deferred_charge(session, charge.id)


async def list_deferred_charges(
    session: AsyncSession,
    *,
    status: str | None = None,
    audit_id: uuid.UUID | None = None,
) -> list[DeferredAuditCharge]:
    query = select(DeferredAuditCharge).options(*_charge_load_options())
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
    for recipient in charge.recipients:
        for split in recipient.splits:
            if split.run_id is None and split.applied_at is None:
                split.applied_at = now
                split.adjustment_id = None
        recipient.splits_remaining = 0
    charge.status = "cancelled"
    charge.updated_at = now

    await session.flush()
    await session.commit()
    await session.refresh(charge)
    return await _load_deferred_charge(session, charge.id)


async def apply_pending_splits_for_run(
    session: AsyncSession,
    run: PayrollRun,
    period_end: date,
    *,
    period_start: date | None = None,
) -> list[DeferredAuditChargeSplit]:
    result = await session.scalars(
        select(DeferredAuditChargeRecipient)
        .options(*_recipient_load_options())
        .join(DeferredAuditCharge, DeferredAuditChargeRecipient.charge_id == DeferredAuditCharge.id)
        .where(
            DeferredAuditChargeRecipient.collapsed_at.is_(None),
            DeferredAuditChargeRecipient.splits_remaining > 0,
            DeferredAuditCharge.status.in_(PENDING_STATUSES),
        )
        .order_by(DeferredAuditChargeRecipient.created_at, DeferredAuditChargeRecipient.id)
        .with_for_update(skip_locked=True)
    )
    recipients = list(result.all())
    applied: list[DeferredAuditChargeSplit] = []
    touched_charges: set[DeferredAuditCharge] = set()
    category: PayrollAdjustmentCategory | None = None
    now = datetime.now(UTC)

    for recipient in recipients:
        line = await session.scalar(
            select(PayrollLine)
            .where(
                PayrollLine.run_id == run.id,
                PayrollLine.employee_id == recipient.employee_id,
            )
            .limit(1)
        )
        if line is None:
            continue

        split = first_unapplied_split(recipient)
        if split is None:
            recipient.splits_remaining = 0
            touched_charges.add(recipient.charge)
            continue

        # Привязка к дате: если у штрафа задан стартовый период, доля N садится в
        # период start + 7*(N-1) дней и не применяется в более ранних прогонах.
        charge = recipient.charge
        if charge.start_period_start is not None and period_start is not None:
            target_period_start = charge.start_period_start + timedelta(
                days=7 * (split.split_index - 1)
            )
            if period_start < target_period_start:
                continue

        if split.amount > 0:
            if category is None:
                category = await _get_or_create_deferred_category(session)
            adjustment = PayrollAdjustment(
                id=uuid.uuid4(),
                employee_id=recipient.employee_id,
                work_date=period_end,
                type="penalty",
                category_id=category.id,
                custom_label=None,
                amount=split.amount,
                comment=deferred_adjustment_comment(recipient.charge, split),
                created_by_user_id=None,
                created_by_label=DEFERRED_AUDIT_CREATED_BY_LABEL,
                created_at=now,
                updated_at=now,
            )
            session.add(adjustment)
            split.adjustment_id = adjustment.id

        split.run_id = run.id
        split.applied_at = now
        recipient.splits_remaining = max(0, recipient.splits_remaining - 1)
        touched_charges.add(recipient.charge)
        applied.append(split)

    for charge in touched_charges:
        await _update_charge_status(charge)

    if applied:
        await session.flush()
    return applied


async def collapse_deferred_charges_on_dismissal(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    run: PayrollRun,
    period_end: date,
) -> list[DeferredAuditChargeRecipient]:
    result = await session.scalars(
        select(DeferredAuditChargeRecipient)
        .options(*_recipient_load_options())
        .join(DeferredAuditCharge, DeferredAuditChargeRecipient.charge_id == DeferredAuditCharge.id)
        .where(
            DeferredAuditChargeRecipient.employee_id == employee_id,
            DeferredAuditChargeRecipient.collapsed_at.is_(None),
            DeferredAuditChargeRecipient.splits_remaining > 0,
            DeferredAuditCharge.status.in_(PENDING_STATUSES),
        )
        .order_by(DeferredAuditChargeRecipient.created_at, DeferredAuditChargeRecipient.id)
        .with_for_update(skip_locked=True)
    )
    recipients = list(result.all())
    collapsed: list[DeferredAuditChargeRecipient] = []
    category: PayrollAdjustmentCategory | None = None
    now = datetime.now(UTC)

    for recipient in recipients:
        unapplied = [
            split
            for split in sorted(recipient.splits, key=lambda item: item.split_index)
            if split.run_id is None and split.applied_at is None
        ]
        if not unapplied:
            continue
        total = _money(sum((split.amount for split in unapplied), Decimal("0")))
        adjustment_id: uuid.UUID | None = None
        if total > 0:
            if category is None:
                category = await _get_or_create_deferred_category(session)
            adjustment = PayrollAdjustment(
                id=uuid.uuid4(),
                employee_id=employee_id,
                work_date=period_end,
                type="penalty",
                category_id=category.id,
                custom_label=None,
                amount=total,
                comment=deferred_dismissal_comment(recipient.charge, len(unapplied)),
                created_by_user_id=None,
                created_by_label=DEFERRED_AUDIT_DISMISSAL_CREATED_BY_LABEL,
                created_at=now,
                updated_at=now,
            )
            session.add(adjustment)
            adjustment_id = adjustment.id

        for split in unapplied:
            split.run_id = run.id
            split.adjustment_id = adjustment_id
            split.applied_at = now
        recipient.splits_remaining = 0
        recipient.collapsed_at = now
        recipient.collapse_run_id = run.id
        recipient.collapse_adjustment_id = adjustment_id
        collapsed.append(recipient)

    for charge in {recipient.charge for recipient in collapsed}:
        await _update_charge_status(charge)

    if collapsed:
        await session.flush()
    return collapsed


def split_charge_amount(total_amount: Decimal, splits_count: int) -> list[Decimal]:
    total = _money(total_amount)
    base = _money(total / Decimal(splits_count))
    amounts = [base for _index in range(max(0, splits_count - 1))]
    amounts.append(_money(total - base * Decimal(max(0, splits_count - 1))))
    return amounts


def first_unapplied_split(
    recipient: DeferredAuditChargeRecipient,
) -> DeferredAuditChargeSplit | None:
    return next(
        (
            split
            for split in sorted(recipient.splits, key=lambda item: item.split_index)
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


def deferred_dismissal_comment(charge: DeferredAuditCharge, remaining_count: int) -> str:
    return (
        f"Распределённый штраф ревизии {source_audit_date_string(charge)}: "
        f"схлопнут при увольнении, остаток {remaining_count} долей."
    )


def source_audit_date_string(charge: DeferredAuditCharge) -> str:
    audit_date = getattr(getattr(charge, "source_audit", None), "business_date", None)
    if isinstance(audit_date, date):
        return audit_date.isoformat()
    return str(charge.source_audit_id)


async def _recipient_shares_for_charge(
    session: AsyncSession,
    *,
    audit: InventoryAudit,
    allocation_group: str,
    total_penalty_amount: Decimal,
) -> tuple[list[Employee], dict[uuid.UUID, Decimal]]:
    period_start, period_end = audit_period(audit)
    exclusions = {exclusion.employee_id for exclusion in getattr(audit, "employee_exclusions", [])}

    chefs: list[Employee] = []
    admins: list[Employee] = []
    if allocation_group in {"chefs", "common"}:
        chefs = _included_employees(
            await _load_period_employees(
                session,
                position="Повар",
                period_start=period_start,
                period_end=period_end,
            ),
            exclusions,
        )
    if allocation_group in {"admins", "common"}:
        admins = _included_employees(
            await _load_period_employees(
                session,
                position="Кассир",
                period_start=period_start,
                period_end=period_end,
            ),
            exclusions,
        )

    total = _money(total_penalty_amount)
    if allocation_group == "chefs":
        recipients = sorted(chefs, key=lambda employee: employee.full_name)
        return recipients, split_amount_evenly(total, recipients)
    if allocation_group == "admins":
        recipients = sorted(admins, key=lambda employee: employee.full_name)
        return recipients, split_amount_evenly(total, recipients)

    chefs_amount = _money(total / Decimal("2"))
    admins_amount = _money(total - chefs_amount)
    shares = split_amount_evenly(chefs_amount, chefs) | split_amount_evenly(admins_amount, admins)
    recipients = sorted([*chefs, *admins], key=lambda employee: employee.full_name)
    return recipients, shares


def _included_employees(employees: list[Employee], exclusions: set[uuid.UUID]) -> list[Employee]:
    return [employee for employee in employees if employee.id not in exclusions]


def _build_recipient(
    employee: Employee,
    recipient_share: Decimal,
    splits_count: int,
) -> DeferredAuditChargeRecipient:
    amounts = split_charge_amount(recipient_share, splits_count)
    recipient = DeferredAuditChargeRecipient(
        employee_id=employee.id,
        per_split_amount=amounts[0] if amounts else Decimal("0"),
        splits_remaining=splits_count,
    )
    recipient.splits = [
        DeferredAuditChargeSplit(split_index=index, amount=amount)
        for index, amount in enumerate(amounts, start=1)
    ]
    return recipient


async def _update_charge_status(charge: DeferredAuditCharge) -> None:
    if charge.status == "cancelled":
        return
    recipients = list(getattr(charge, "recipients", []) or [])
    if recipients and all(recipient.splits_remaining == 0 for recipient in recipients):
        charge.status = "applied"
    elif any(
        split.run_id is not None or split.adjustment_id is not None or split.applied_at is not None
        for recipient in recipients
        for split in recipient.splits
    ):
        charge.status = "partially_applied"
    else:
        charge.status = "pending"
    charge.updated_at = datetime.now(UTC)


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
        id=uuid.uuid4(),
        type="penalty",
        code=DEFERRED_AUDIT_CATEGORY_CODE,
        display_name=DEFERRED_AUDIT_CATEGORY_NAME,
        sort_order=55,
        is_active=True,
    )
    session.add(category)
    await session.flush()
    return category


async def _load_audit_with_exclusions(
    session: AsyncSession,
    audit_id: uuid.UUID,
) -> InventoryAudit:
    audit = await session.scalar(
        select(InventoryAudit)
        .options(selectinload(InventoryAudit.employee_exclusions))
        .where(InventoryAudit.id == audit_id)
    )
    if audit is None:
        raise DeferredChargeNotFoundError("Ревизия не найдена")
    return audit


async def _load_audit_item_with_position(
    session: AsyncSession,
    item_id: uuid.UUID,
) -> InventoryAuditItem:
    item = await session.scalar(
        select(InventoryAuditItem)
        .options(selectinload(InventoryAuditItem.position))
        .where(InventoryAuditItem.id == item_id)
    )
    if item is None:
        raise DeferredChargeNotFoundError("Позиция ревизии не найдена")
    return item


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
        selectinload(DeferredAuditCharge.recipients).selectinload(
            DeferredAuditChargeRecipient.splits
        ),
        selectinload(DeferredAuditCharge.recipients).selectinload(
            DeferredAuditChargeRecipient.employee
        ),
        selectinload(DeferredAuditCharge.source_audit),
        selectinload(DeferredAuditCharge.source_item),
        selectinload(DeferredAuditCharge.created_by),
    )


def _recipient_load_options() -> tuple[Any, ...]:
    return (
        selectinload(DeferredAuditChargeRecipient.splits),
        selectinload(DeferredAuditChargeRecipient.charge).selectinload(
            DeferredAuditCharge.source_audit
        ),
        selectinload(DeferredAuditChargeRecipient.charge)
        .selectinload(DeferredAuditCharge.recipients)
        .selectinload(DeferredAuditChargeRecipient.splits),
    )


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(MONEY, rounding=ROUND_HALF_UP)

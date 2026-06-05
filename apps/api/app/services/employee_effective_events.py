from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Employee,
    EmployeeAllowanceEvent,
    EmployeePendingIikoAction,
    EmployeePositionEvent,
)
from app.services.employee_position_service import position_at
from app.services.staff_taxonomy import canonical_position_name, validate_premiums

ALLOWANCE_TYPES = frozenset({"senior", "deputy_senior"})
ALLOWANCE_FIELD_BY_TYPE = {
    "senior": "is_senior",
    "deputy_senior": "is_deputy_senior",
}


class EmployeeEffectiveEventError(ValueError):
    pass


class EmployeeEffectiveEventNotFoundError(LookupError):
    pass


async def get_position_on_date(
    session: AsyncSession,
    employee_id: uuid.UUID,
    on_date: date,
) -> str | None:
    return await position_at(session, employee_id, on_date)


async def get_position_event_on_date(
    session: AsyncSession,
    employee_id: uuid.UUID,
    on_date: date,
) -> EmployeePositionEvent | None:
    return await session.scalar(
        select(EmployeePositionEvent)
        .where(
            EmployeePositionEvent.employee_id == employee_id,
            EmployeePositionEvent.effective_from <= on_date,
            or_(
                EmployeePositionEvent.effective_to.is_(None),
                EmployeePositionEvent.effective_to > on_date,
            ),
        )
        .order_by(EmployeePositionEvent.effective_from.desc())
        .limit(1)
    )


async def list_position_events(
    session: AsyncSession,
    employee_id: uuid.UUID,
) -> list[EmployeePositionEvent]:
    result = await session.scalars(
        select(EmployeePositionEvent)
        .where(EmployeePositionEvent.employee_id == employee_id)
        .order_by(EmployeePositionEvent.effective_from.desc())
    )
    return list(result.all())


async def set_position(
    session: AsyncSession,
    employee_id: uuid.UUID,
    position: str,
    *,
    effective_from: date,
    comment: str | None = None,
) -> EmployeePositionEvent:
    canonical_position = canonical_position_name(position)
    if canonical_position is None:
        raise EmployeeEffectiveEventError("Должность не входит в канонический список")

    await _get_employee(session, employee_id)
    existing = await _position_event_starting_on(session, employee_id, effective_from)
    if existing is not None:
        existing.position = canonical_position
        existing.comment = comment
        event = existing
    else:
        active = await get_position_event_on_date(session, employee_id, effective_from)
        if active is not None and active.effective_from < effective_from:
            active.effective_to = effective_from
        next_event = await _next_position_event_after(session, employee_id, effective_from)
        event = EmployeePositionEvent(
            employee_id=employee_id,
            position=canonical_position,
            effective_from=effective_from,
            effective_to=next_event.effective_from if next_event is not None else None,
            comment=comment,
        )
        session.add(event)

    await session.flush()
    return event


async def get_allowances_on_date(
    session: AsyncSession,
    employee_id: uuid.UUID,
    on_date: date,
) -> dict[str, bool]:
    employee = await session.get(Employee, employee_id)
    result = {
        "is_senior": bool(employee.is_senior) if employee is not None else False,
        "is_deputy_senior": bool(employee.is_deputy_senior) if employee is not None else False,
    }
    events = await get_allowance_events_on_date(session, employee_id, on_date)
    for event in events:
        field = ALLOWANCE_FIELD_BY_TYPE[event.allowance_type]
        result[field] = bool(event.is_enabled)
    return result


async def get_allowance_events_on_date(
    session: AsyncSession,
    employee_id: uuid.UUID,
    on_date: date,
) -> list[EmployeeAllowanceEvent]:
    result = await session.scalars(
        select(EmployeeAllowanceEvent)
        .where(
            EmployeeAllowanceEvent.employee_id == employee_id,
            EmployeeAllowanceEvent.effective_from <= on_date,
            or_(
                EmployeeAllowanceEvent.effective_to.is_(None),
                EmployeeAllowanceEvent.effective_to > on_date,
            ),
        )
        .order_by(
            EmployeeAllowanceEvent.allowance_type,
            EmployeeAllowanceEvent.effective_from.desc(),
        )
    )
    events_by_type: dict[str, EmployeeAllowanceEvent] = {}
    for event in result.all():
        events_by_type.setdefault(event.allowance_type, event)
    return list(events_by_type.values())


async def list_allowance_events(
    session: AsyncSession,
    employee_id: uuid.UUID,
) -> list[EmployeeAllowanceEvent]:
    result = await session.scalars(
        select(EmployeeAllowanceEvent)
        .where(EmployeeAllowanceEvent.employee_id == employee_id)
        .order_by(
            EmployeeAllowanceEvent.allowance_type,
            EmployeeAllowanceEvent.effective_from.desc(),
        )
    )
    return list(result.all())


async def set_allowance(
    session: AsyncSession,
    employee_id: uuid.UUID,
    allowance_type: str,
    is_enabled: bool,
    *,
    effective_from: date,
    comment: str | None = None,
) -> EmployeeAllowanceEvent:
    if allowance_type not in ALLOWANCE_TYPES:
        raise EmployeeEffectiveEventError("Invalid allowance type")

    employee = await _get_employee(session, employee_id)
    if is_enabled:
        position = await get_position_on_date(session, employee_id, effective_from)
        try:
            validate_premiums(
                position,
                is_senior=allowance_type == "senior",
                is_deputy_senior=allowance_type == "deputy_senior",
            )
        except ValueError as exc:
            raise EmployeeEffectiveEventError(str(exc)) from exc

    existing = await _allowance_event_starting_on(
        session,
        employee_id,
        allowance_type,
        effective_from,
    )
    if existing is not None:
        existing.is_enabled = is_enabled
        existing.comment = comment
        event = existing
    else:
        active = await _allowance_event_on_date(
            session,
            employee_id,
            allowance_type,
            effective_from,
        )
        if active is not None and active.effective_from < effective_from:
            active.effective_to = effective_from
        next_event = await _next_allowance_event_after(
            session,
            employee_id,
            allowance_type,
            effective_from,
        )
        event = EmployeeAllowanceEvent(
            employee_id=employee_id,
            allowance_type=allowance_type,
            is_enabled=is_enabled,
            effective_from=effective_from,
            effective_to=next_event.effective_from if next_event is not None else None,
            comment=comment,
        )
        session.add(event)

    if effective_from <= date.today():
        field = ALLOWANCE_FIELD_BY_TYPE[allowance_type]
        active_today = await _allowance_event_on_date(
            session,
            employee_id,
            allowance_type,
            date.today(),
        )
        if active_today is None or active_today.id == event.id:
            setattr(employee, field, bool(is_enabled))

    await session.flush()
    return event


async def schedule_iiko_position_update(
    session: AsyncSession,
    employee: Employee,
    *,
    position: str,
    effective_on: date,
    related_entity_type: str | None = None,
    related_entity_id: uuid.UUID | None = None,
) -> EmployeePendingIikoAction:
    pending = await session.scalar(
        select(EmployeePendingIikoAction).where(
            EmployeePendingIikoAction.employee_id == employee.id,
            EmployeePendingIikoAction.action_type == "update_position",
            EmployeePendingIikoAction.status == "pending",
            EmployeePendingIikoAction.effective_on == effective_on,
        )
    )
    payload = {"iiko_id": employee.iiko_id, "position": position}
    if pending is not None:
        pending.payload = payload
        pending.related_entity_type = related_entity_type
        pending.related_entity_id = related_entity_id
        action = pending
    else:
        action = EmployeePendingIikoAction(
            employee_id=employee.id,
            action_type="update_position",
            status="pending",
            effective_on=effective_on,
            payload=payload,
            related_entity_type=related_entity_type,
            related_entity_id=related_entity_id,
        )
        session.add(action)
    await session.flush()
    return action


async def apply_due_iiko_actions(
    session: AsyncSession,
    *,
    today: date | None = None,
    limit: int = 100,
) -> dict[str, int]:
    from app.services.iiko_sync import update_iiko_employee

    due_date = today or date.today()
    result = await session.scalars(
        select(EmployeePendingIikoAction)
        .where(
            EmployeePendingIikoAction.status == "pending",
            EmployeePendingIikoAction.effective_on <= due_date,
        )
        .order_by(EmployeePendingIikoAction.effective_on, EmployeePendingIikoAction.created_at)
        .limit(limit)
    )
    actions = list(result.all())
    applied = 0
    failed = 0
    now = datetime.now(UTC)

    for action in actions:
        action.attempts += 1
        employee = await session.get(Employee, action.employee_id)
        if employee is None:
            action.status = "cancelled"
            action.last_error = "Employee not found"
            failed += 1
            continue
        try:
            if action.action_type == "update_position":
                position = str(action.payload.get("position") or "").strip()
                if not position:
                    raise EmployeeEffectiveEventError("Pending action has no target position")
                await update_iiko_employee(session, iiko_id=employee.iiko_id, position=position)
            action.status = "applied"
            action.applied_at = now
            action.last_error = None
            applied += 1
        except Exception as exc:  # noqa: BLE001 - keep the worker moving through the queue.
            action.status = "failed"
            action.last_error = str(exc)[:500]
            failed += 1

    await session.commit()
    return {"applied": applied, "failed": failed, "processed": len(actions)}


def position_event_snapshot(event: EmployeePositionEvent) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "employee_id": str(event.employee_id),
        "position": event.position,
        "effective_from": event.effective_from.isoformat(),
        "effective_to": event.effective_to.isoformat() if event.effective_to else None,
        "comment": event.comment,
    }


def allowance_event_snapshot(event: EmployeeAllowanceEvent) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "employee_id": str(event.employee_id),
        "allowance_type": event.allowance_type,
        "is_enabled": event.is_enabled,
        "effective_from": event.effective_from.isoformat(),
        "effective_to": event.effective_to.isoformat() if event.effective_to else None,
        "comment": event.comment,
    }


async def _get_employee(session: AsyncSession, employee_id: uuid.UUID) -> Employee:
    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise EmployeeEffectiveEventNotFoundError("Employee not found")
    return employee


async def _position_event_starting_on(
    session: AsyncSession,
    employee_id: uuid.UUID,
    effective_from: date,
) -> EmployeePositionEvent | None:
    return await session.scalar(
        select(EmployeePositionEvent).where(
            EmployeePositionEvent.employee_id == employee_id,
            EmployeePositionEvent.effective_from == effective_from,
        )
    )


async def _next_position_event_after(
    session: AsyncSession,
    employee_id: uuid.UUID,
    effective_from: date,
) -> EmployeePositionEvent | None:
    return await session.scalar(
        select(EmployeePositionEvent)
        .where(
            EmployeePositionEvent.employee_id == employee_id,
            EmployeePositionEvent.effective_from > effective_from,
        )
        .order_by(EmployeePositionEvent.effective_from)
        .limit(1)
    )


async def _allowance_event_on_date(
    session: AsyncSession,
    employee_id: uuid.UUID,
    allowance_type: str,
    on_date: date,
) -> EmployeeAllowanceEvent | None:
    return await session.scalar(
        select(EmployeeAllowanceEvent)
        .where(
            EmployeeAllowanceEvent.employee_id == employee_id,
            EmployeeAllowanceEvent.allowance_type == allowance_type,
            EmployeeAllowanceEvent.effective_from <= on_date,
            or_(
                EmployeeAllowanceEvent.effective_to.is_(None),
                EmployeeAllowanceEvent.effective_to > on_date,
            ),
        )
        .order_by(EmployeeAllowanceEvent.effective_from.desc())
        .limit(1)
    )


async def _allowance_event_starting_on(
    session: AsyncSession,
    employee_id: uuid.UUID,
    allowance_type: str,
    effective_from: date,
) -> EmployeeAllowanceEvent | None:
    return await session.scalar(
        select(EmployeeAllowanceEvent).where(
            EmployeeAllowanceEvent.employee_id == employee_id,
            EmployeeAllowanceEvent.allowance_type == allowance_type,
            EmployeeAllowanceEvent.effective_from == effective_from,
        )
    )


async def _next_allowance_event_after(
    session: AsyncSession,
    employee_id: uuid.UUID,
    allowance_type: str,
    effective_from: date,
) -> EmployeeAllowanceEvent | None:
    return await session.scalar(
        select(EmployeeAllowanceEvent)
        .where(
            EmployeeAllowanceEvent.employee_id == employee_id,
            EmployeeAllowanceEvent.allowance_type == allowance_type,
            EmployeeAllowanceEvent.effective_from > effective_from,
        )
        .order_by(EmployeeAllowanceEvent.effective_from)
        .limit(1)
    )


def normalize_allowance_items(items: Iterable[EmployeeAllowanceEvent]) -> dict[str, bool]:
    flags = {"is_senior": False, "is_deputy_senior": False}
    for item in items:
        flags[ALLOWANCE_FIELD_BY_TYPE[item.allowance_type]] = bool(item.is_enabled)
    return flags

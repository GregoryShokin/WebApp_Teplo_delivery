from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor
from app.models import EmployeeChangeEvent
from app.services import employee_change_events as employee_change_event_service

NOTICE_GIVEN = "notice_given"
NOTICE_CANCELLED = "notice_cancelled"
DISMISS = "dismiss"
NOTICE_ACTIVE_TYPES = frozenset({NOTICE_GIVEN, NOTICE_CANCELLED, DISMISS})


class NoticeAlreadyExistsError(ValueError):
    pass


class NoticeNotFoundError(ValueError):
    pass


async def record_notice(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    notice_date: date,
    comment: str | None,
    actor: CurrentActor,
) -> EmployeeChangeEvent:
    active_notice = await get_active_notice(session, employee_id, date.max)
    if active_notice is not None:
        raise NoticeAlreadyExistsError("У сотрудника уже есть активное уведомление об уходе")

    now = datetime.now(UTC)
    return await employee_change_event_service.add_employee_change_event(
        session,
        employee_id=employee_id,
        change_type=NOTICE_GIVEN,
        source="app",
        changed_at=now,
        effective_from=notice_date,
        actor_label=employee_change_event_service.actor_label_from_roles(actor.roles),
        summary="Сотрудник уведомил об уходе",
        after_value={"notice_date": notice_date.isoformat()},
        comment=comment,
        payroll_impact=False,
    )


async def cancel_notice(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    comment: str | None,
    actor: CurrentActor,
) -> EmployeeChangeEvent:
    active_notice = await get_active_notice(session, employee_id, date.max)
    if active_notice is None:
        raise NoticeNotFoundError("Активное уведомление об уходе не найдено")

    now = datetime.now(UTC)
    today = date.today()
    return await employee_change_event_service.add_employee_change_event(
        session,
        employee_id=employee_id,
        change_type=NOTICE_CANCELLED,
        source="app",
        changed_at=now,
        effective_from=today,
        actor_label=employee_change_event_service.actor_label_from_roles(actor.roles),
        summary="Уведомление об уходе отменено",
        before_value={"notice_date": active_notice.effective_from.isoformat()}
        if active_notice.effective_from
        else None,
        after_value={"cancelled": True},
        comment=comment,
        payroll_impact=False,
    )


async def get_active_notice(
    session: AsyncSession,
    employee_id: uuid.UUID,
    as_of: date,
) -> EmployeeChangeEvent | None:
    """Return latest notice_given without a later notice_cancelled or dismiss event."""
    result = await session.scalars(
        select(EmployeeChangeEvent)
        .where(
            EmployeeChangeEvent.employee_id == employee_id,
            EmployeeChangeEvent.change_type.in_(sorted(NOTICE_ACTIVE_TYPES)),
        )
        .order_by(EmployeeChangeEvent.changed_at.desc(), EmployeeChangeEvent.created_at.desc())
    )
    return _active_notice_from_events(result.all(), as_of)


async def list_active_notices(
    session: AsyncSession,
    employee_ids: Iterable[uuid.UUID],
    as_of: date,
) -> dict[uuid.UUID, EmployeeChangeEvent]:
    ids = list(employee_ids)
    if not ids:
        return {}
    result = await session.scalars(
        select(EmployeeChangeEvent)
        .where(
            EmployeeChangeEvent.employee_id.in_(ids),
            EmployeeChangeEvent.change_type.in_(sorted(NOTICE_ACTIVE_TYPES)),
        )
        .order_by(
            EmployeeChangeEvent.employee_id,
            EmployeeChangeEvent.changed_at.desc(),
            EmployeeChangeEvent.created_at.desc(),
        )
    )
    events_by_employee: dict[uuid.UUID, list[EmployeeChangeEvent]] = {}
    for event in result.all():
        if event.employee_id is None:
            continue
        events_by_employee.setdefault(event.employee_id, []).append(event)

    active: dict[uuid.UUID, EmployeeChangeEvent] = {}
    for employee_id, events in events_by_employee.items():
        notice = _active_notice_from_events(events, as_of)
        if notice is not None:
            active[employee_id] = notice
    return active


def _active_notice_from_events(
    events: Iterable[EmployeeChangeEvent],
    as_of: date,
) -> EmployeeChangeEvent | None:
    sorted_events = sorted(
        (event for event in events if event.change_type in NOTICE_ACTIVE_TYPES),
        key=lambda event: (
            event.changed_at or datetime.min.replace(tzinfo=UTC),
            event.created_at or datetime.min.replace(tzinfo=UTC),
        ),
        reverse=True,
    )
    for event in sorted_events:
        if event.change_type in {NOTICE_CANCELLED, DISMISS}:
            return None
        if event.effective_from is not None and event.effective_from > as_of:
            continue
        if event.change_type == NOTICE_GIVEN:
            return event
    return None

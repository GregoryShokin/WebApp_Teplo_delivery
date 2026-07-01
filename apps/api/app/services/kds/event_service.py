"""Журнал тапов упаковки: идемпотентная запись (офлайн-буфер), откат, свёртка колонок.

Денормализованные ``kds_order.{ready_at,waiting_courier_at,handed_to_courier_at}`` всегда
ПЕРЕсчитываются из журнала свёрткой по ``effective_at`` — поэтому порядок флаша офлайн-буфера
и откаты безопасны. Откат = новая строка ``action=rollback`` (журнал append-only, без DELETE).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KdsOrder, KdsOrderEvent
from app.models.kds import (
    EVENT_ACTION_VALUES,
    TAP_TYPE_VALUES,
    KdsEventAction,
    KdsEventSource,
    KdsTapType,
)

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

_TAP_COLUMN = {
    KdsTapType.READY.value: "ready_at",
    KdsTapType.WAITING_COURIER.value: "waiting_courier_at",
    KdsTapType.HANDED.value: "handed_to_courier_at",
}


@dataclass(slots=True)
class TapEventInput:
    client_event_id: str
    iiko_order_id: str
    event_type: str
    effective_at: datetime
    action: str = KdsEventAction.SET.value


@dataclass(slots=True)
class EventResult:
    client_event_id: str
    status: str  # applied | duplicate | rejected
    reason: str | None = None
    event_id: str | None = None


async def _get_or_create_order(session: AsyncSession, iiko_order_id: str) -> KdsOrder:
    order = await session.scalar(
        select(KdsOrder).where(KdsOrder.iiko_order_id == iiko_order_id)
    )
    if order is not None:
        return order
    # Тап опередил снимок из iikoCloud — заводим минимальную карточку, снимок дополнит её.
    order = KdsOrder(
        id=uuid.uuid4(),
        iiko_order_id=iiko_order_id,
        work_date=datetime.now(MOSCOW_TZ).date(),
        is_active=True,
    )
    session.add(order)
    await session.flush()
    return order


async def recompute_tap_columns(
    session: AsyncSession,
    order: KdsOrder,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    events = (
        await session.scalars(
            select(KdsOrderEvent)
            .where(KdsOrderEvent.kds_order_id == order.id)
            .order_by(KdsOrderEvent.effective_at, KdsOrderEvent.recorded_at)
        )
    ).all()
    current: dict[str, datetime | None] = {value: None for value in _TAP_COLUMN}
    for event in events:
        if event.event_type not in current:
            continue
        if event.action == KdsEventAction.ROLLBACK.value:
            current[event.event_type] = None
        else:  # set | edit
            current[event.event_type] = event.effective_at
    for tap_type, column in _TAP_COLUMN.items():
        setattr(order, column, current[tap_type])
    if actor_user_id is not None:
        order.last_actor_user_id = actor_user_id


async def record_events(
    session: AsyncSession,
    items: list[TapEventInput],
    *,
    actor_user_id: uuid.UUID | None,
    source: str = KdsEventSource.KIOSK.value,
) -> list[EventResult]:
    results: list[EventResult] = []
    if not items:
        return results

    client_ids = [item.client_event_id for item in items if item.client_event_id]
    existing_ids: set[str] = set()
    if client_ids:
        existing_ids = set(
            (
                await session.scalars(
                    select(KdsOrderEvent.client_event_id).where(
                        KdsOrderEvent.client_event_id.in_(client_ids)
                    )
                )
            ).all()
        )

    affected: dict[str, KdsOrder] = {}
    for item in items:
        if not item.client_event_id:
            results.append(EventResult("", "rejected", reason="no client_event_id"))
            continue
        if item.event_type not in TAP_TYPE_VALUES or item.action not in EVENT_ACTION_VALUES:
            results.append(EventResult(item.client_event_id, "rejected", reason="bad event"))
            continue
        if item.client_event_id in existing_ids:
            results.append(EventResult(item.client_event_id, "duplicate"))
            continue

        order = affected.get(item.iiko_order_id) or await _get_or_create_order(
            session, item.iiko_order_id
        )
        affected[item.iiko_order_id] = order
        event = KdsOrderEvent(
            id=uuid.uuid4(),
            kds_order_id=order.id,
            iiko_order_id=item.iiko_order_id,
            event_type=item.event_type,
            action=item.action,
            effective_at=item.effective_at,
            actor_user_id=actor_user_id,
            source=source,
            client_event_id=item.client_event_id,
        )
        session.add(event)
        existing_ids.add(item.client_event_id)  # защита от дублей внутри одного батча
        results.append(EventResult(item.client_event_id, "applied", event_id=str(event.id)))

    await session.flush()
    for order in affected.values():
        await recompute_tap_columns(session, order, actor_user_id=actor_user_id)
    await session.commit()
    return results


async def rollback_event(
    session: AsyncSession,
    event_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None,
) -> KdsOrder | None:
    target = await session.get(KdsOrderEvent, event_id)
    if target is None:
        return None
    order = await session.get(KdsOrder, target.kds_order_id)
    if order is None:
        return None

    rollback = KdsOrderEvent(
        id=uuid.uuid4(),
        kds_order_id=order.id,
        iiko_order_id=target.iiko_order_id,
        event_type=target.event_type,
        action=KdsEventAction.ROLLBACK.value,
        effective_at=datetime.now(UTC),
        actor_user_id=actor_user_id,
        source=KdsEventSource.MANAGER.value,
        client_event_id=f"rollback:{target.id}",
        previous_value={
            "undone_event_id": str(target.id),
            "event_type": target.event_type,
            "effective_at": target.effective_at.isoformat() if target.effective_at else None,
        },
    )
    session.add(rollback)
    await session.flush()
    await recompute_tap_columns(session, order, actor_user_id=actor_user_id)
    await session.commit()
    return order

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_permission
from app.db.session import get_session
from app.models import KdsOrder
from app.schemas.kds import (
    KdsEventResult,
    KdsNudgeThresholds,
    KdsQueueOrder,
    KdsQueueResponse,
    KdsRecordEventsRequest,
    KdsRecordEventsResponse,
)
from app.services.kds import dashboard_service, event_service
from app.services.kds.event_service import TapEventInput
from app.services.settings_service import SettingNotFoundError, get_setting_model

router = APIRouter()
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

QUEUE_READ = (Depends(require_permission("kds.queue.read")),)
QUEUE_WRITE = (Depends(require_permission("kds.queue.write")),)
DASHBOARD_READ = (Depends(require_permission("kitchen.speed.read")),)

_READY_NUDGE_KEY = "kds.ready_nudge_minutes"
_WAITING_NUDGE_KEY = "kds.waiting_courier_nudge_minutes"


async def _read_int_setting(session: AsyncSession, key: str, default: int) -> int:
    try:
        setting = await get_setting_model(session, key)
    except SettingNotFoundError:
        return default
    try:
        return int(setting.value)
    except (TypeError, ValueError):
        return default


def _stage(order: KdsOrder) -> str:
    if order.handed_to_courier_at is not None:
        return "handed"
    if order.waiting_courier_at is not None:
        return "waiting_courier"
    if order.ready_at is not None:
        return "ready"
    return "pending"


def _to_queue_order(order: KdsOrder) -> KdsQueueOrder:
    return KdsQueueOrder(
        id=order.id,
        iiko_order_id=order.iiko_order_id,
        order_number=order.order_number,
        status=order.status,
        is_preorder=order.is_preorder,
        has_hot_item=order.has_hot_item,
        complete_before=order.complete_before,
        when_created=order.when_created,
        ready_at=order.ready_at,
        waiting_courier_at=order.waiting_courier_at,
        handed_to_courier_at=order.handed_to_courier_at,
        stage=_stage(order),
    )


@router.get("/queue", response_model=KdsQueueResponse, dependencies=QUEUE_READ)
async def get_queue(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> KdsQueueResponse:
    today = datetime.now(MOSCOW_TZ).date()
    # Доска показывает всё, что нужно отдать СЕГОДНЯ, независимо от даты создания: ASAP-заказы
    # сегодняшнего дня + предзаказы со сроком (completeBefore) сегодня, даже созданные вчера
    # (иначе кросс-полуночный предзаказ с work_date=вчера не виден). Дальние предзаказы
    # (срок в будущем) не засоряют доску до своего дня.
    day_start_utc = datetime.combine(today, time.min, tzinfo=MOSCOW_TZ).astimezone(UTC)
    next_day_start_utc = day_start_utc + timedelta(days=1)
    orders = (
        await session.scalars(
            select(KdsOrder)
            .where(
                KdsOrder.is_active.is_(True),
                or_(
                    and_(
                        KdsOrder.complete_before.is_not(None),
                        KdsOrder.complete_before >= day_start_utc,
                        KdsOrder.complete_before < next_day_start_utc,
                    ),
                    and_(KdsOrder.complete_before.is_(None), KdsOrder.work_date == today),
                ),
            )
            .order_by(
                KdsOrder.complete_before.is_(None),
                KdsOrder.complete_before,
                KdsOrder.when_created,
            )
        )
    ).all()
    nudge = KdsNudgeThresholds(
        ready_minutes=await _read_int_setting(session, _READY_NUDGE_KEY, 10),
        waiting_courier_minutes=await _read_int_setting(session, _WAITING_NUDGE_KEY, 15),
    )
    return KdsQueueResponse(
        server_time=datetime.now(MOSCOW_TZ),
        nudge=nudge,
        orders=[_to_queue_order(order) for order in orders],
    )


@router.post("/events", response_model=KdsRecordEventsResponse, dependencies=QUEUE_WRITE)
async def post_events(
    payload: KdsRecordEventsRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> KdsRecordEventsResponse:
    items = [
        TapEventInput(
            client_event_id=event.client_event_id,
            iiko_order_id=event.iiko_order_id,
            event_type=event.event_type,
            effective_at=event.effective_at,
            action=event.action,
        )
        for event in payload.events
    ]
    results = await event_service.record_events(session, items, actor_user_id=actor.user_id)
    return KdsRecordEventsResponse(
        results=[
            KdsEventResult(
                client_event_id=result.client_event_id,
                status=result.status,
                reason=result.reason,
                event_id=result.event_id,
            )
            for result in results
        ]
    )


@router.post("/events/{event_id}/rollback", response_model=KdsQueueOrder, dependencies=QUEUE_WRITE)
async def post_rollback(
    event_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> KdsQueueOrder:
    order = await event_service.rollback_event(session, event_id, actor_user_id=actor.user_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Событие не найдено")
    return _to_queue_order(order)


@router.get("/dashboard", dependencies=DASHBOARD_READ)
async def get_dashboard(
    session: Annotated[AsyncSession, Depends(get_session)],
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
) -> dict[str, object]:
    today = datetime.now(MOSCOW_TZ).date()
    resolved_to = date_to or today
    resolved_from = date_from or (resolved_to - timedelta(days=6))
    if resolved_from > resolved_to:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "date_from позже date_to")
    return await dashboard_service.compute_dashboard(
        session, date_from=resolved_from, date_to=resolved_to
    )

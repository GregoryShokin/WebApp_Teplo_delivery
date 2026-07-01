from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class KdsQueueOrder(BaseModel):
    id: uuid.UUID
    iiko_order_id: str
    order_number: str | None
    status: str | None
    is_preorder: bool
    has_hot_item: bool
    complete_before: datetime | None
    when_created: datetime | None
    ready_at: datetime | None
    waiting_courier_at: datetime | None
    handed_to_courier_at: datetime | None
    stage: str  # pending | ready | waiting_courier | handed


class KdsNudgeThresholds(BaseModel):
    ready_minutes: int
    waiting_courier_minutes: int


class KdsQueueResponse(BaseModel):
    server_time: datetime
    nudge: KdsNudgeThresholds
    orders: list[KdsQueueOrder]


class KdsTapEvent(BaseModel):
    client_event_id: str = Field(min_length=1, max_length=200)
    iiko_order_id: str = Field(min_length=1)
    event_type: str  # ready | waiting_courier | handed
    effective_at: datetime
    action: str = "set"  # set | rollback | edit


class KdsRecordEventsRequest(BaseModel):
    events: list[KdsTapEvent] = Field(min_length=1, max_length=200)


class KdsEventResult(BaseModel):
    client_event_id: str
    status: str  # applied | duplicate | rejected
    reason: str | None = None
    event_id: str | None = None


class KdsRecordEventsResponse(BaseModel):
    results: list[KdsEventResult]

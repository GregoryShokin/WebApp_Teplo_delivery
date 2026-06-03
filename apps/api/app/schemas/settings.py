from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AppSettingRead(BaseModel):
    id: uuid.UUID
    key: str
    value: Any
    value_type: str
    category: str
    display_name: str
    description: str | None = None
    widget_type: str
    widget_options: Any | None = None
    unit: str | None = None
    is_critical: bool = False
    updated_at: datetime
    updated_by_user_id: uuid.UUID | None = None
    updated_by_user_name: str | None = None


class AppSettingUpdate(BaseModel):
    value: Any = Field(...)


class AppSettingHistoryRead(BaseModel):
    id: uuid.UUID
    setting_id: uuid.UUID
    old_value: Any | None
    new_value: Any
    changed_at: datetime
    changed_by_user_id: uuid.UUID | None = None
    changed_by_user_name: str | None = None


class SubstitutePairRead(BaseModel):
    from_position: str
    to_position: str
    add_to_schedule: bool = False


class SubstitutePairsRead(BaseModel):
    pairs: list[SubstitutePairRead]


class SubstitutePairsUpdate(BaseModel):
    pairs: list[SubstitutePairRead] = Field(default_factory=list)

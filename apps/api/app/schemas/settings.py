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
    description: str | None = None
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

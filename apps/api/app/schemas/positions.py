from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PositionArchetype = Literal["okladnik", "production_percent", "shift_pool", "courier", "none"]
PositionPermissionGroup = Literal[
    "administration", "cooks", "cashiers", "auxiliary", "couriers", "none"
]
PositionScheduleType = Literal["SESSION", "FIXED", "HOURS"]


class PositionBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    archetype: PositionArchetype = "none"
    permission_group: PositionPermissionGroup = "none"
    # Участие в выдаче доступов: должность попадает в список «Права должностей»
    # и получает привязанную роль-движок прав.
    participates_in_access: bool = False
    schedule_type: PositionScheduleType | None = None


class PositionCreateRequest(PositionBase):
    pass


class PositionUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    archetype: PositionArchetype | None = None
    permission_group: PositionPermissionGroup | None = None
    participates_in_access: bool | None = None
    schedule_type: PositionScheduleType | None = None


class PositionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    iiko_role_id: str | None
    iiko_role_code: str | None
    archetype: str
    permission_group: str
    participates_in_access: bool
    access_role_code: str | None
    status: str
    schedule_type: str
    employee_count: int = 0
    created_at: datetime
    updated_at: datetime


class PositionListRead(BaseModel):
    positions: list[PositionRead]


class PositionIikoSyncResult(BaseModel):
    linked: int
    imported: int
    provisioned: int = 0
    positions: list[PositionRead]


class PositionChangeEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    position_id: uuid.UUID | None
    position_name: str
    action: str
    before_value: Any | None
    after_value: Any | None
    actor_user_id: uuid.UUID | None
    created_at: datetime

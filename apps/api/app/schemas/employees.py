from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

EmployeeStatus = str


class EmployeeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    iiko_id: str
    position: str | None = None
    category: str | None = None
    default_cooking_station: str | None = None
    is_senior: bool = False
    is_deputy_senior: bool = False
    status: EmployeeStatus
    hire_date: date | None = None
    fire_date: date | None = None
    iiko_sync_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class EmployeePatch(BaseModel):
    position: str | None = Field(default=None, max_length=160)
    category: str | None = Field(default=None, max_length=160)
    default_cooking_station: str | None = Field(default=None, max_length=160)
    is_senior: bool | None = None
    is_deputy_senior: bool | None = None
    hire_date: date | None = None
    fire_date: date | None = None


class SyncResultRead(BaseModel):
    created: int
    updated: int
    deactivated: int


class ErrorDetail(BaseModel):
    detail: str | dict[str, Any]

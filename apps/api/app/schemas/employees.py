from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

EmployeeStatus = str


class EmployeeRoleAssignmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    payroll_role: str
    category: str
    is_primary: bool
    effective_from: date
    effective_to: date | None = None
    created_at: datetime
    updated_at: datetime


class EmployeeRoleAssignmentCreate(BaseModel):
    payroll_role: str = Field(max_length=64)
    category: str = Field(max_length=64)
    is_primary: bool = False
    effective_from: date | None = None


class EmployeeRoleAssignmentPatch(BaseModel):
    payroll_role: str | None = Field(default=None, max_length=64)
    category: str | None = Field(default=None, max_length=64)
    is_primary: bool | None = None


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
    fire_reason: str | None = None
    iiko_sync_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    assignments: list[EmployeeRoleAssignmentRead] = Field(default_factory=list)


class EmployeeDismissRequest(BaseModel):
    fire_date: date | None = None
    reason: str | None = Field(default=None, max_length=1000)


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

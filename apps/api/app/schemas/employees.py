from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    pin_set_at: datetime | None = None
    iiko_sync_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    assignments: list[EmployeeRoleAssignmentRead] = Field(default_factory=list)


class EmployeeDismissRequest(BaseModel):
    fire_date: date | None = None
    reason: str | None = Field(default=None, max_length=1000)


class EmployeeCreateRoleRequest(BaseModel):
    payroll_role: str = Field(min_length=1, max_length=64)
    category: str = Field(min_length=1, max_length=64)
    is_primary: bool = False


class EmployeeCreateRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)
    pin_code: str
    iiko_role_id: str = Field(min_length=1, max_length=128)
    roles: list[EmployeeCreateRoleRequest] = Field(default_factory=list)
    is_senior: bool = False
    is_deputy_senior: bool = False

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized.split()) < 2:
            raise ValueError("Укажите минимум два слова в ФИО")
        return normalized

    @field_validator("pin_code")
    @classmethod
    def validate_pin_code(cls, value: str) -> str:
        if not value.isdigit() or len(value) != 4:
            raise ValueError("ПИН-код должен состоять из 4 цифр")
        return value

    @model_validator(mode="after")
    def validate_roles(self) -> EmployeeCreateRequest:
        if not self.roles:
            return self
        primary_count = sum(1 for role in self.roles if role.is_primary)
        if primary_count != 1:
            raise ValueError("Ровно одна роль должна быть основной")
        payroll_roles = [role.payroll_role for role in self.roles]
        if len(set(payroll_roles)) != len(payroll_roles):
            raise ValueError("Роли не должны повторяться")
        return self


class EmployeePinChangeRequest(BaseModel):
    pin_code: str

    @field_validator("pin_code")
    @classmethod
    def validate_pin_code(cls, value: str) -> str:
        if not value.isdigit() or len(value) != 4:
            raise ValueError("ПИН-код должен состоять из 4 цифр")
        return value


class IikoEmployeeRoleRead(BaseModel):
    id: str
    name: str
    code: str | None = None
    deleted: bool = False


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

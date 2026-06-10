from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class PayrollAdjustmentCategoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    code: str | None = Field(default=None, max_length=96)
    display_name: str = Field(min_length=1, max_length=200)
    default_amount: Decimal | None = Field(default=None, ge=0)
    description: str | None = Field(default=None, max_length=1000)
    sort_order: int = 0


class PayrollAdjustmentCategoryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    default_amount: Decimal | None = Field(default=None, ge=0)
    description: str | None = Field(default=None, max_length=1000)
    is_active: bool | None = None
    sort_order: int | None = None


class PayrollAdjustmentCategoryRead(BaseModel):
    id: uuid.UUID
    type: str
    code: str
    display_name: str
    description: str | None = None
    default_amount: str | None = None
    is_active: bool
    sort_order: int
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PayrollAdjustmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_id: uuid.UUID
    work_date: date
    type: str
    category_id: uuid.UUID | None = None
    custom_label: str | None = Field(default=None, max_length=200)
    amount: Decimal = Field(gt=0)
    comment: str | None = Field(default=None, max_length=1000)
    # Роль, к которой относится начисление (для сотрудников с несколькими ролями).
    # Если не указана — берётся основная должность сотрудника.
    role: str | None = Field(default=None, max_length=160)


class PayrollAdjustmentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_id: uuid.UUID | None = None
    work_date: date | None = None
    type: str | None = None
    category_id: uuid.UUID | None = None
    custom_label: str | None = Field(default=None, max_length=200)
    amount: Decimal | None = Field(default=None, gt=0)
    comment: str | None = Field(default=None, max_length=1000)
    role: str | None = Field(default=None, max_length=160)


class PayrollAdjustmentRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_full_name: str
    employee_position: str
    work_date: date
    type: str
    role: str | None = None
    category_id: uuid.UUID | None = None
    category_display_name: str | None = None
    custom_label: str | None = None
    amount: str
    comment: str | None = None
    created_by_user_id: uuid.UUID | None = None
    created_by_label: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    is_locked: bool = False

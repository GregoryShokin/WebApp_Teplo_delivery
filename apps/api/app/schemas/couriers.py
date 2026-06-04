from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import CourierCategory, CourierDepositTransactionType, CourierEvaluationSource


class CourierCategoryAssignRequest(BaseModel):
    category: CourierCategory
    effective_from: date
    actor_id: uuid.UUID


class CourierCategoryAssignmentRead(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    id: int | None = None
    employee_id: uuid.UUID
    category: CourierCategory
    effective_from: date
    effective_to: date | None = None
    created_by: uuid.UUID
    created_at: datetime | None = None


class CourierCategoryRow(BaseModel):
    employee_id: uuid.UUID
    full_name: str
    status: str
    category: str | None = None


class CourierDepositSettingsRead(BaseModel):
    target_amount: int
    withhold_primary: int
    withhold_secondary: int
    auto_withhold_enabled: bool


class CourierDepositSettingsUpdate(BaseModel):
    target_amount: int | None = Field(default=None, ge=0)
    withhold_primary: int | None = Field(default=None, ge=0)
    withhold_secondary: int | None = Field(default=None, ge=0)
    auto_withhold_enabled: bool | None = None


class CourierDepositTransactionRead(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    id: int | None = None
    account_employee_id: uuid.UUID
    transaction_type: CourierDepositTransactionType | str
    amount_cents: int
    transaction_date: date
    comment: str | None = None
    created_by: uuid.UUID
    created_at: datetime | None = None


class CourierDepositRow(BaseModel):
    employee_id: uuid.UUID
    full_name: str
    status: str
    category: str | None = None
    target_amount_cents: int
    opening_balance_cents: int
    opening_date: date
    balance_cents: int
    progress_pct: float
    remaining_to_target_cents: int
    last_transaction: CourierDepositTransactionRead | None = None


class CourierDepositAccountRead(BaseModel):
    employee_id: uuid.UUID
    target_amount_cents: int
    opening_balance_cents: int
    opening_date: date
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CourierDepositCardRead(BaseModel):
    account: CourierDepositAccountRead
    balance_cents: int
    transactions: list[CourierDepositTransactionRead]


class CourierDepositOpeningUpdate(BaseModel):
    amount_cents: int = Field(ge=0)
    opening_date: date
    actor_id: uuid.UUID


class CourierDepositTransactionCreate(BaseModel):
    transaction_type: CourierDepositTransactionType
    amount_cents: int = Field(gt=0)
    transaction_date: date
    comment: str | None = Field(default=None, max_length=2000)
    actor_id: uuid.UUID


class CourierEvaluationCriterionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    label: str
    score: int
    is_active: bool
    display_order: int


class CourierEvaluationCreate(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    courier_employee_id: uuid.UUID
    criterion_id: int
    evaluated_at: date | None = None
    comment: str | None = Field(default=None, max_length=2000)
    actor_id: uuid.UUID
    source: CourierEvaluationSource = CourierEvaluationSource.WEB


class CourierEvaluationUpdate(BaseModel):
    criterion_id: int | None = None
    evaluated_at: date | None = None
    comment: str | None = Field(default=None, max_length=2000)
    actor_id: uuid.UUID


class CourierEvaluationRead(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    id: int | None = None
    courier_employee_id: uuid.UUID
    criterion_id: int
    score_snapshot: int
    comment: str | None = None
    evaluated_at: date
    source: CourierEvaluationSource | str
    created_by: uuid.UUID
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deleted_at: datetime | None = None


class CourierEvaluationTopCriterion(BaseModel):
    criterion_id: int
    code: str
    label: str
    count: int


class CourierEvaluationMonthlyAggregate(BaseModel):
    courier_employee_id: uuid.UUID
    month: str
    score_sum: int
    positive_count: int
    negative_count: int
    neutral_count: int
    top_criteria: list[CourierEvaluationTopCriterion]


class CourierScheduleEntryRead(BaseModel):
    id: int | None = None
    courier_employee_id: uuid.UUID
    work_date: date
    planned_start_at: datetime
    planned_end_at: datetime
    comment: str | None = None
    created_by: uuid.UUID
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CourierScheduleUpsert(BaseModel):
    planned_start_at: datetime
    planned_end_at: datetime
    comment: str | None = Field(default=None, max_length=2000)
    actor_id: uuid.UUID


CourierDepositStatus = Literal["active", "fired", "all"]
CourierDepositCategory = Literal["primary", "secondary", "all"]

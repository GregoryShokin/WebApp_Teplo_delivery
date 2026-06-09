from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import CourierDepositTransactionType, CourierEvaluationSource


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
    created_by_name: str | None = None
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


class CourierDepositTransactionCreate(BaseModel):
    transaction_type: CourierDepositTransactionType
    amount_cents: int = Field(gt=0)
    transaction_date: date
    comment: str | None = Field(default=None, max_length=2000)


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
    source: CourierEvaluationSource = CourierEvaluationSource.WEB


class CourierEvaluationUpdate(BaseModel):
    criterion_id: int | None = None
    evaluated_at: date | None = None
    comment: str | None = Field(default=None, max_length=2000)


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


class EvaluationMonthlySummary(BaseModel):
    score_sum: int
    positive_count: int
    negative_count: int
    neutral_count: int
    top_criteria: list[CourierEvaluationTopCriterion]


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
    category: Literal["primary", "secondary"]
    planned_start_at: datetime
    planned_end_at: datetime
    comment: str | None = None
    created_by: uuid.UUID
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CourierScheduleUpsert(BaseModel):
    category: Literal["primary", "secondary"]
    planned_start_at: datetime | None = None
    planned_end_at: datetime | None = None
    comment: str | None = Field(default=None, max_length=2000)


class CourierScheduleMatchedEntry(BaseModel):
    id: int | None = None
    courier_employee_id: uuid.UUID
    work_date: date
    work_status: Literal["working", "reserve"] | None = None
    category: Literal["primary", "secondary"] | None = None
    planned_start_at: datetime | None = None
    planned_end_at: datetime | None = None
    comment: str | None = None
    status: (
        Literal[
            "matched_primary",
            "short_primary",
            "no_show_primary",
            "matched_secondary",
            "short_secondary",
            "no_show_secondary",
            "helping",
            "not_counted",
            "not_started",
        ]
        | None
    ) = None
    late_minutes: int | None = None
    worked_minutes: int | None = None
    deliveries_count: int | None = None
    iiko_shift_id: int | None = None
    opened_at: datetime | None = None
    closed_at: datetime | None = None


CourierDepositStatus = Literal["active", "fired", "all"]
CourierDepositCategory = Literal["primary", "secondary", "all"]
CourierWorkStatus = Literal["working", "reserve"]
CourierWorkStatusFilter = Literal["working", "reserve", "all"]

KpiThreshold = Literal["green", "yellow", "red"]


class KpiValue(BaseModel):
    value: float | int | None
    threshold: KpiThreshold | None = None


class CourierKPI(BaseModel):
    courier_id: uuid.UUID
    courier_name: str
    speed_minutes: KpiValue
    discipline_percent: KpiValue
    productivity: KpiValue
    help_count: int
    deliveries_total: int
    primary_shifts_worked: int
    secondary_shifts_worked: int
    primary_shifts_planned: int


class CourierStatisticsRow(BaseModel):
    kpi: CourierKPI
    evaluations: EvaluationMonthlySummary


class CourierShiftBrief(BaseModel):
    id: int
    opened_at: datetime
    closed_at: datetime | None = None
    attendance_type: str
    worked_minutes: int | None = None


class CourierEvaluationDetailRead(CourierEvaluationRead):
    criterion_code: str | None = None
    criterion_label: str | None = None
    author_name: str | None = None


class CourierDisciplineBreakdown(BaseModel):
    planned: int
    worked: int
    help: int
    no_show: int


class CourierStatisticsDetail(BaseModel):
    courier_iiko_id: str
    kpi: CourierKPI
    evaluations: EvaluationMonthlySummary
    latest_evaluations: list[CourierEvaluationDetailRead]
    last_shift: CourierShiftBrief | None = None
    discipline_breakdown: CourierDisciplineBreakdown


class CourierListRow(BaseModel):
    employee_id: uuid.UUID
    full_name: str
    iiko_id: str
    status: str
    work_status: CourierWorkStatus | None
    open_shift_now: bool
    primary_shifts_in_month: int
    secondary_shifts_in_month: int


class CourierListSummary(BaseModel):
    active_total: int
    fired_this_month: int
    open_shift_now_total: int
    reserve_total: int


class CourierListResponse(BaseModel):
    month: str
    summary: CourierListSummary
    rows: list[CourierListRow]

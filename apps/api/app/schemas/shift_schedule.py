from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ALLOWANCE_RECIPIENT_ROLES = {"senior", "deputy_senior", "none"}


class ScheduleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_start: date
    date_end: date
    notes: str | None = Field(default=None, max_length=4000)

    @field_validator("notes")
    @classmethod
    def strip_notes(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_date_range(self) -> ScheduleCreateRequest:
        if self.date_end < self.date_start:
            raise ValueError("Дата окончания не может быть раньше даты начала")
        return self


class SchedulePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notes: str | None = Field(default=None, max_length=4000)

    @field_validator("notes")
    @classmethod
    def strip_notes(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        return normalized or None


class ScheduledShiftUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    business_date: date
    employee_id: uuid.UUID
    payroll_role: str | None = Field(default=None, max_length=64)
    station_code: str | None = Field(default=None, max_length=64)
    planned_start_at: datetime | None = None
    planned_end_at: datetime | None = None
    comment_private: str | None = Field(default=None, max_length=2000)

    @field_validator("payroll_role", "station_code", "comment_private")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        return normalized or None


class CopyWeekRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_date: date
    to_date: date


class ScheduledShiftRead(BaseModel):
    id: uuid.UUID
    business_date: date
    employee_id: uuid.UUID
    employee_full_name: str
    payroll_role: str
    station_code: str | None = None
    planned_start_at: datetime
    planned_end_at: datetime
    planned_hours: Decimal
    comment_private: str | None = None


class ScheduleRead(BaseModel):
    id: uuid.UUID
    date_start: date
    date_end: date
    status: str
    notes: str | None = None
    published_at: datetime | None = None
    superseded_by_id: uuid.UUID | None = None
    created_by_label: str | None = None
    shifts: list[ScheduledShiftRead] = Field(default_factory=list)


class ScheduleLedgerEntryRead(BaseModel):
    id: uuid.UUID
    business_date: date
    employee_id: uuid.UUID
    employee_full_name: str
    position: str
    payroll_role: str | None = None
    station_code: str | None = None
    opened_at: datetime
    closed_at: datetime | None = None
    minutes_worked: int
    is_closed: bool


class EmployeeRosterAllowanceRead(BaseModel):
    senior: bool
    deputy: bool


class EmployeeRosterAvailableRoleRead(BaseModel):
    payroll_role: str
    category: str
    is_primary: bool
    default_station_code: str | None = None


class EmployeeRosterRow(BaseModel):
    id: uuid.UUID
    full_name: str
    position: str
    primary_payroll_role: str | None = None
    default_cooking_station: str | None = None
    available_roles: list[EmployeeRosterAvailableRoleRead] = Field(default_factory=list)
    allowances: EmployeeRosterAllowanceRead


class CopyWeekResponse(BaseModel):
    copied: int


class RevenueForecastHistoryPoint(BaseModel):
    date: date
    amount: Decimal | None
    included: bool


class RevenueForecastRead(BaseModel):
    business_date: date
    weekday: int
    method_code: str
    history_window_weeks: int
    history_points: list[RevenueForecastHistoryPoint]
    base_average_amount: Decimal | None
    season_coeff: Decimal
    event_coeff: Decimal
    manual_override_amount: Decimal | None
    manual_override_reason: str | None
    manual_override_set_by_label: str | None
    manual_override_set_at: datetime | None
    forecast_amount: Decimal | None
    quality_status: str
    event_review_recommended: bool
    computed_at: datetime | None


class RevenueForecastOverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0)
    reason: str | None = Field(default=None, max_length=2000)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        return normalized or None


class RevenueForecastRecomputeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_from: date
    date_to: date
    force_refresh_iiko: bool = False

    @model_validator(mode="after")
    def validate_date_range(self) -> RevenueForecastRecomputeRequest:
        if self.date_to < self.date_from:
            raise ValueError("Дата окончания не может быть раньше даты начала")
        if (self.date_to - self.date_from).days > 62:
            raise ValueError("Период прогноза не может быть длиннее 62 дней")
        return self


class RevenueForecastRecomputeResponse(BaseModel):
    recomputed: int


class CashierAllowanceOverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    business_date: date
    recipient_role: str
    recipient_employee_id: uuid.UUID | None = None
    comment: str | None = Field(default=None, max_length=2000)

    @field_validator("recipient_role")
    @classmethod
    def validate_recipient_role(cls, value: str) -> str:
        if value not in ALLOWANCE_RECIPIENT_ROLES:
            raise ValueError("recipient_role должен быть senior, deputy_senior или none")
        return value

    @field_validator("comment")
    @classmethod
    def strip_comment(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_recipient_consistency(self) -> CashierAllowanceOverrideRequest:
        if self.recipient_role == "none" and self.recipient_employee_id is not None:
            raise ValueError("Для recipient_role='none' не указывайте recipient_employee_id")
        if (
            self.recipient_role in {"senior", "deputy_senior"}
            and self.recipient_employee_id is None
        ):
            raise ValueError("Для senior/deputy_senior укажите recipient_employee_id")
        return self


class ShiftAllowanceOverrideRead(BaseModel):
    id: uuid.UUID
    shift_schedule_id: uuid.UUID
    business_date: date
    position: str
    recipient_employee_id: uuid.UUID | None
    recipient_role: str
    comment: str | None
    set_by_user_id: uuid.UUID | None
    set_at: datetime
    created_at: datetime
    updated_at: datetime


class AllowanceCandidateRead(BaseModel):
    employee_id: uuid.UUID
    full_name: str
    is_senior: bool
    is_deputy_senior: bool
    is_planned: bool
    is_actual: bool
    minutes_worked: int


class AllowanceAssignmentRead(BaseModel):
    business_date: date
    position: str = "Кассир"
    recipient_employee_id: uuid.UUID | None
    recipient_full_name: str | None
    recipient_role: str
    reason: str
    candidates: list[AllowanceCandidateRead]
    has_manual_override: bool


class ShiftCostEstimateRead(BaseModel):
    id: uuid.UUID
    scheduled_shift_id: uuid.UUID
    business_date: date
    employee_id: uuid.UUID
    employee_full_name: str
    planned_hours: Decimal
    base_salary_estimate: Decimal
    weekday_premium_estimate: Decimal
    allowance_estimate: Decimal
    revenue_percent_estimate: Decimal
    fund_accrual_estimate: Decimal
    total_cost_estimate: Decimal
    quality_status: str
    quality_reasons: list[str]
    breakdown: dict[str, Any]


class PayrollForecastRunRead(BaseModel):
    id: uuid.UUID
    shift_schedule_id: uuid.UUID
    run_at: datetime
    run_by_label: str | None
    status: str
    total_revenue_forecast: Decimal | None
    total_shift_cost_estimate: Decimal | None
    fot_to_revenue_pct: Decimal | None
    fot_warning_threshold_pct: Decimal
    shifts_total: int
    shifts_with_warnings: int
    estimates: list[ShiftCostEstimateRead] = Field(default_factory=list)


class PlanFactDayRowRead(BaseModel):
    business_date: date
    planned_shifts: int
    planned_hours: str
    planned_cost: str | None
    planned_revenue: str | None
    actual_shifts: int
    actual_hours: str | None
    actual_cost: str | None
    actual_revenue: str | None
    hours_deviation_pct: str | None
    cost_deviation_pct: str | None
    revenue_deviation_pct: str | None
    deviation_status: str
    deviation_flags: list[str] = Field(default_factory=list)
    planned_cashier_allowance: dict[str, Any] | None = None
    actual_cashier_allowance: dict[str, Any] | None = None


class PlanFactEmployeeRowRead(BaseModel):
    employee_id: uuid.UUID
    full_name: str
    position: str
    planned_shifts: int
    planned_hours: str
    planned_cost: str | None
    actual_shifts: int
    actual_hours: str | None
    actual_cost: str | None
    hours_deviation_pct: str | None
    cost_deviation_pct: str | None
    deviation_status: str


class PlanFactSummaryRead(BaseModel):
    schedule: dict[str, Any]
    fact_availability: str
    covered_dates: list[date]
    planned: dict[str, Any]
    actual: dict[str, Any] | None
    deviation: dict[str, Any] | None
    warning_threshold_pct: str
    by_date: list[PlanFactDayRowRead]
    by_employee: list[PlanFactEmployeeRowRead]
    sources: dict[str, Any]

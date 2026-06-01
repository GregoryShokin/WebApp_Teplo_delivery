from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    station_code: str | None = Field(default=None, max_length=64)
    planned_start_at: datetime
    planned_end_at: datetime
    comment_private: str | None = Field(default=None, max_length=2000)

    @field_validator("station_code", "comment_private")
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


class EmployeeRosterAllowanceRead(BaseModel):
    senior: bool
    deputy: bool


class EmployeeRosterRow(BaseModel):
    id: uuid.UUID
    full_name: str
    position: str
    primary_payroll_role: str | None = None
    default_cooking_station: str | None = None
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

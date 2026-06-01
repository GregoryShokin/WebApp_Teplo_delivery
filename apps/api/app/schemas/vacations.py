from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class VacationPeriodRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_full_name: str
    date_start: date
    date_end: date
    days_count: int
    status: str
    comment: str | None
    created_by_label: str | None
    created_at: datetime


class VacationBalanceRead(BaseModel):
    employee_id: uuid.UUID
    year: int
    limit: int
    used: int
    remaining: int
    periods: list[VacationPeriodRead]


class ShiftConflict(BaseModel):
    shift_id: uuid.UUID
    business_date: date
    schedule_id: uuid.UUID
    schedule_status: str


class VacationConflictResponse(BaseModel):
    detail: str
    conflicting_shifts: list[ShiftConflict]


class VacationPeriodCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_id: uuid.UUID
    date_start: date
    date_end: date
    comment: str | None = Field(default=None, max_length=2000)
    force_remove_conflicting_shifts: bool = False

    @field_validator("comment")
    @classmethod
    def strip_comment(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_date_range(self) -> VacationPeriodCreate:
        if self.date_end < self.date_start:
            raise ValueError("Дата окончания не может быть раньше даты начала")
        return self


class VacationPeriodPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_start: date | None = None
    date_end: date | None = None
    comment: str | None = Field(default=None, max_length=2000)
    force_remove_conflicting_shifts: bool = False

    @field_validator("comment")
    @classmethod
    def strip_comment(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        return normalized or None


class VacationRosterRow(BaseModel):
    employee_id: uuid.UUID
    employee_full_name: str
    position: str
    year: int
    limit: int
    used: int
    remaining: int
    periods: list[VacationPeriodRead]

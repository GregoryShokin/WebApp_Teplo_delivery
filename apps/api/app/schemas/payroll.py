from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class PayrollPeriodRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    period_type: str
    start_date: date
    end_date: date
    payroll_date: date
    status: str
    finalized_at: datetime | None = None
    finalized_by_user_id: uuid.UUID | None = None


class PayrollRunCreate(BaseModel):
    period_id: uuid.UUID | None = None


class PayrollRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    period_id: uuid.UUID
    started_at: datetime
    finished_at: datetime | None = None
    status: str
    blocking_issues: list[dict[str, Any]]
    summary: dict[str, Any]
    period: PayrollPeriodRead | None = None


class PayrollLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    employee_id: uuid.UUID
    role: str
    base_pay: float
    premium: float
    percent_pay: float
    fund_accrual: float
    deduction: float
    total_payable: float
    components: dict[str, Any]


class ShiftLedgerBuildRequest(BaseModel):
    work_date: date


class ShiftLedgerPatch(BaseModel):
    payroll_role: str
    category: str


class ShiftLedgerEntryRead(BaseModel):
    id: uuid.UUID
    work_date: date
    employee_id: uuid.UUID
    employee_name: str
    employee_iiko_id: str
    payroll_role: str | None = None
    category: str | None = None
    source: str
    opened_at: datetime
    closed_at: datetime | None = None
    notes: str | None = None
    is_resolved: bool

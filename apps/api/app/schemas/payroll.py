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
    force_refresh: bool = False


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
    deposit_withholding: float = 0
    deposit_payout: float = 0
    ndfl_deduction: float = 0
    total_payable: float
    deposit_excluded_for_run: bool = False
    deposit_exclusion_reason: str | None = None
    components: dict[str, Any]


class PayrollLineDepositOverridePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deposit_excluded_for_run: bool
    deposit_exclusion_reason: str | None = None


class ShiftLedgerBuildRequest(BaseModel):
    work_date: date


class ShiftLedgerPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payroll_role: str


class ShiftLedgerAvailableRoleRead(BaseModel):
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
    status: str
    available_roles: list[ShiftLedgerAvailableRoleRead]


class ShiftLedgerMatrixDayHeaderRead(BaseModel):
    date: date
    is_today: bool


class ShiftLedgerMatrixSummaryRead(BaseModel):
    earliest_open: datetime | None = None
    latest_close: datetime | None = None
    shift_count: int


class ShiftLedgerMatrixShiftRead(BaseModel):
    ledger_entry_id: uuid.UUID
    opened_at: datetime
    closed_at: datetime | None = None
    payroll_role: str | None = None
    category: str | None = None
    is_resolved: bool
    status: str
    payroll_locked: bool


class ShiftLedgerMatrixDayRead(BaseModel):
    date: date
    payroll_locked: bool
    available_roles: list[ShiftLedgerAvailableRoleRead]
    summary: ShiftLedgerMatrixSummaryRead
    shifts: list[ShiftLedgerMatrixShiftRead]


class ShiftLedgerMatrixEmployeeRead(BaseModel):
    id: uuid.UUID
    full_name: str
    iiko_id: str
    days: list[ShiftLedgerMatrixDayRead]


class ShiftLedgerMatrixRead(BaseModel):
    selected_date: date
    start_date: date
    end_date: date
    days: list[ShiftLedgerMatrixDayHeaderRead]
    employees: list[ShiftLedgerMatrixEmployeeRead]

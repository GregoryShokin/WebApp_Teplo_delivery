from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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


class PayrollRunUnfinalize(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str


class PayrollRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    period_id: uuid.UUID
    started_at: datetime
    finished_at: datetime | None = None
    status: str
    blocking_issues: list[dict[str, Any]]
    summary: dict[str, Any]
    is_imported_legacy: bool = False
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
    vacation_pay: float
    ndfl_withheld: float = 0
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


class DeferredChargeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_audit_id: uuid.UUID
    source_item_id: uuid.UUID
    total_penalty_amount: Decimal = Field(gt=0)
    splits_count: int = Field(ge=1, le=24)
    reason: str = Field(min_length=1, max_length=500)


class DeferredChargeSplitRead(BaseModel):
    id: uuid.UUID
    split_index: int
    amount: str
    run_id: uuid.UUID | None = None
    adjustment_id: uuid.UUID | None = None
    applied_at: datetime | None = None


class DeferredChargeRecipientRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str | None = None
    employee_position: str | None = None
    per_split_amount: str
    splits_remaining: int
    collapsed_at: datetime | None = None
    collapse_run_id: uuid.UUID | None = None
    splits: list[DeferredChargeSplitRead]


class DeferredChargeRead(BaseModel):
    id: uuid.UUID
    source_audit_id: uuid.UUID
    source_item_id: uuid.UUID | None = None
    source_audit_date: date | None = None
    source_item_name: str | None = None
    allocation_group: str
    total_penalty_amount: str
    splits_count: int
    status: str
    reason: str
    created_by_name: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    recipients: list[DeferredChargeRecipientRead]


class PayrollPersonalReportPeriodRead(BaseModel):
    period_id: uuid.UUID
    run_id: uuid.UUID
    run_status: str
    role: str
    period_start: date
    period_end: date
    base_pay: float
    premium: float
    percent_pay: float
    vacation_pay: float
    ndfl_withheld: float
    fund_accrual: float
    deduction: float
    deposit_withholding: float
    bonus_total: float
    penalty_total: float
    total_payable: float


class PayrollPersonalReportDailyRead(BaseModel):
    date: date
    base_pay: float
    percent_pay: float
    premium: float
    vacation_pay: float
    ndfl_withheld: float
    fund_accrual: float
    deposit_in: float
    deposit_out: float
    penalty: float
    audit_penalty: float
    comment: str | None = None


class PayrollPersonalReportAdjustmentRead(BaseModel):
    id: uuid.UUID
    type: str
    work_date: date
    category_id: uuid.UUID | None = None
    category_name: str
    custom_label: str | None = None
    amount: float
    comment: str | None = None


class PayrollPersonalReportDepositTransactionRead(BaseModel):
    id: uuid.UUID
    transaction_type: str
    amount: float
    created_at: datetime
    run_id: uuid.UUID | None = None


class PayrollPersonalReportTotalsRead(BaseModel):
    base_pay: float
    premium: float
    percent_pay: float
    vacation_pay: float
    ndfl_withheld: float
    fund_accrual: float
    deduction: float
    deposit_withholding: float
    bonus_total: float
    penalty_total: float
    audit_penalty_total: str
    total_payable: float


class PayrollPersonalReportRead(BaseModel):
    employee_id: uuid.UUID
    employee_name: str
    employee_position: str | None = None
    date_from: date
    date_to: date
    periods: list[PayrollPersonalReportPeriodRead]
    daily: list[PayrollPersonalReportDailyRead]
    opening_balance: str
    closing_balance: str
    shifts_count: int
    adjustments: list[PayrollPersonalReportAdjustmentRead]
    deposit_transactions: list[PayrollPersonalReportDepositTransactionRead]
    totals: PayrollPersonalReportTotalsRead


class PayrollAggregateTotals(BaseModel):
    base_pay: str
    premium: str
    percent_pay: str
    vacation_pay: str
    fund_accrual: str
    deduction: str
    bonus_total: str
    penalty_total: str
    deposit_withheld: str
    ndfl_total: str
    gross: str
    total_payable: str


class PayrollAggregatePeriod(BaseModel):
    period_id: str
    period_start: date
    period_end: date
    run_id: str
    run_status: str
    lines_count: int
    total_payable: str
    base_pay: str
    premium: str
    percent_pay: str
    deduction: str
    fund_accrual: str


class PayrollAggregateRead(BaseModel):
    date_from: date
    date_to: date
    employees_count: int
    runs_count: int
    totals: PayrollAggregateTotals
    periods: list[PayrollAggregatePeriod]


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

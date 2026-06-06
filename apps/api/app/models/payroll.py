from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class PayrollPeriod(Base):
    __tablename__ = "payroll_period"
    __table_args__ = (
        CheckConstraint("period_type = 'week'", name="ck_payroll_period_type_week"),
        UniqueConstraint(
            "period_type",
            "start_date",
            "end_date",
            name="uq_payroll_period_type_start_end",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    period_type: Mapped[str] = mapped_column(String(16), nullable=False, default="week")
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    payroll_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finalized_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="RESTRICT"), nullable=True
    )


class AttendanceEntry(Base):
    __tablename__ = "attendance_entry"
    __table_args__ = (
        CheckConstraint(
            "source in ('iiko', 'manual', 'telegram')",
            name="ck_attendance_entry_source",
        ),
        Index("ix_attendance_entry_period_employee", "period_id", "employee_id"),
        Index("ix_attendance_entry_work_date", "work_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="RESTRICT"), nullable=False
    )
    period_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payroll_period.id", ondelete="CASCADE"), nullable=False
    )
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    minutes_worked: Mapped[int] = mapped_column(nullable=False, default=0)
    station: Mapped[str | None] = mapped_column(String(160), nullable=True)
    role: Mapped[str | None] = mapped_column(String(160), nullable=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="iiko")
    quality_status: Mapped[str] = mapped_column(String(64), nullable=False, default="ok")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class ShiftLedgerEntry(Base):
    __tablename__ = "shift_ledger_entry"
    __table_args__ = (
        CheckConstraint(
            "source in ('schedule', 'manual_correction', 'fallback_primary')",
            name="ck_shift_ledger_entry_source",
        ),
        UniqueConstraint(
            "work_date",
            "employee_id",
            "opened_at",
            name="uq_shift_ledger_entry_work_date_employee_opened",
        ),
        Index("ix_shift_ledger_entry_work_date", "work_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="RESTRICT"), nullable=False
    )
    payroll_role: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="fallback_primary")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_resolved: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )


class PayrollRun(Base):
    __tablename__ = "payroll_run"
    __table_args__ = (Index("ix_payroll_run_period_started", "period_id", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    period_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payroll_period.id", ondelete="RESTRICT"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    blocking_issues: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    is_imported_legacy: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )


class PayrollLine(Base):
    __tablename__ = "payroll_line"
    __table_args__ = (
        UniqueConstraint("run_id", "employee_id", "role", name="uq_payroll_line_run_employee_role"),
        Index("ix_payroll_line_run", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payroll_run.id", ondelete="CASCADE"), nullable=False
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(160), nullable=False)
    base_pay: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    premium: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    percent_pay: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    vacation_pay: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    ndfl_withheld: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0, server_default="0"
    )
    fund_accrual: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    deduction: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    total_payable: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    deposit_excluded_for_run: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    deposit_exclusion_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    components: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )


class DepositAccount(Base):
    __tablename__ = "deposit_account"
    __table_args__ = (
        CheckConstraint("initial_balance >= 0", name="initial_balance_non_negative"),
        UniqueConstraint("employee_id", name="uq_deposit_account_employee"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="RESTRICT"), nullable=False
    )
    balance: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    initial_balance: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0, server_default="0"
    )
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DepositTransaction(Base):
    __tablename__ = "deposit_transaction"
    __table_args__ = (
        CheckConstraint(
            "transaction_type in ("
            "'accrual', 'payout', 'write_off', 'dismissal_payout', 'dismissal_writeoff'"
            ")",
            name="ck_deposit_transaction_type",
        ),
        Index("ix_deposit_transaction_employee_created", "employee_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payroll_run.id", ondelete="SET NULL"), nullable=True
    )
    transaction_type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AccumulationFundAccount(Base):
    __tablename__ = "accumulation_fund_account"
    __table_args__ = (
        CheckConstraint(
            "status in ('active', 'paid_out', 'forfeited')",
            name="ck_accumulation_fund_account_status",
        ),
        UniqueConstraint("employee_id", "year", name="uq_accumulation_fund_employee_year"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="RESTRICT"), nullable=False
    )
    year: Mapped[int] = mapped_column(nullable=False)
    accumulated_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    paid_out_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    forfeited_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0, server_default="0"
    )
    forfeited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    forfeit_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    paid_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class AccumulationFundTransaction(Base):
    __tablename__ = "accumulation_fund_transaction"
    __table_args__ = (
        CheckConstraint(
            "transaction_type in ('accrual', 'payout', 'forfeit', 'initial_balance')",
            name="ck_accumulation_fund_transaction_type",
        ),
        CheckConstraint(
            "(transaction_type = 'initial_balance' and amount >= 0) "
            "or (transaction_type <> 'initial_balance' and amount > 0)",
            name="ck_accumulation_fund_transaction_amount_positive",
        ),
        Index(
            "ix_accumulation_fund_transaction_account_created",
            "account_id",
            "created_at",
        ),
        Index(
            "ix_accumulation_fund_transaction_employee_year",
            "employee_id",
            "year",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accumulation_fund_account.id", ondelete="CASCADE"), nullable=False
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="RESTRICT"), nullable=False
    )
    year: Mapped[int] = mapped_column(nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payroll_run.id", ondelete="SET NULL"), nullable=True
    )
    transaction_type: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    rate_percent: Mapped[Decimal | None] = mapped_column(Numeric(8, 5), nullable=True)
    base_pay_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PayrollRate(Base):
    __tablename__ = "payroll_rate"
    __table_args__ = (
        CheckConstraint(
            "rate_type in ('daily', 'hourly', 'monthly')",
            name="ck_payroll_rate_rate_type",
        ),
        CheckConstraint(
            "category in "
            "('category_1', 'category_2', 'category_3', 'category_4', 'intern', 'freelancer')",
            name="ck_payroll_rate_category_value",
        ),
        CheckConstraint("amount >= 0", name="ck_payroll_rate_amount_non_negative"),
        CheckConstraint(
            "effective_to is null or effective_to > effective_from",
            name="ck_payroll_rate_effective_range",
        ),
        UniqueConstraint(
            "position_group",
            "category",
            "station",
            "rate_type",
            "effective_from",
            name="uq_payroll_rate_natural_effective_from",
        ),
        Index(
            "ix_payroll_rate_current_lookup",
            "position_group",
            "category",
            "station",
            "rate_type",
            "effective_from",
            "effective_to",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    position_group: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    station: Mapped[str | None] = mapped_column(String(160), nullable=True)
    rate_type: Mapped[str] = mapped_column(String(24), nullable=False, default="daily")
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PayrollRoleCategoryAvailability(Base):
    __tablename__ = "payroll_role_category_availability"
    __table_args__ = (
        CheckConstraint(
            "category in "
            "('category_1', 'category_2', 'category_3', 'category_4', 'intern', 'freelancer')",
            name="ck_payroll_role_category_availability_category_value",
        ),
        UniqueConstraint(
            "position_group",
            "category",
            name="uq_payroll_role_category_availability_position_category",
        ),
    )

    position_group: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    category: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )


class PayrollRevenueShare(Base):
    __tablename__ = "payroll_revenue_share"
    __table_args__ = (
        CheckConstraint("percent >= 0", name="ck_payroll_revenue_share_percent_non_negative"),
        CheckConstraint(
            "effective_to is null or effective_to > effective_from",
            name="ck_payroll_revenue_share_effective_range",
        ),
        UniqueConstraint(
            "position_group",
            "category",
            "effective_from",
            name="uq_payroll_revenue_share_natural_effective_from",
        ),
        Index(
            "ix_payroll_revenue_share_current_lookup",
            "position_group",
            "category",
            "effective_from",
            "effective_to",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    position_group: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    percent: Mapped[Decimal] = mapped_column(Numeric(8, 5), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RevenueTier(Base):
    __tablename__ = "revenue_tier"
    __table_args__ = (
        CheckConstraint("min_revenue >= 0", name="ck_revenue_tier_min_revenue_non_negative"),
        CheckConstraint(
            "max_revenue is null or max_revenue > min_revenue",
            name="ck_revenue_tier_revenue_range",
        ),
        CheckConstraint("rate_percent >= 0", name="ck_revenue_tier_rate_percent_non_negative"),
        CheckConstraint(
            "effective_to is null or effective_to > effective_from",
            name="ck_revenue_tier_effective_range",
        ),
        UniqueConstraint(
            "min_revenue",
            "effective_from",
            name="uq_revenue_tier_min_revenue_effective_from",
        ),
        Index(
            "ix_revenue_tier_current_lookup",
            "min_revenue",
            "max_revenue",
            "effective_from",
            "effective_to",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    min_revenue: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    max_revenue: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    rate_percent: Mapped[Decimal] = mapped_column(Numeric(8, 5), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CategoryCoefficient(Base):
    __tablename__ = "category_coefficient"
    __table_args__ = (
        CheckConstraint(
            "category in "
            "('category_1', 'category_2', 'category_3', 'category_4', 'intern', 'freelancer')",
            name="ck_category_coefficient_category_value",
        ),
        CheckConstraint(
            "coefficient >= 0",
            name="ck_category_coefficient_coefficient_non_negative",
        ),
        CheckConstraint(
            "effective_to is null or effective_to > effective_from",
            name="ck_category_coefficient_effective_range",
        ),
        UniqueConstraint(
            "category",
            "effective_from",
            name="uq_category_coefficient_category_effective_from",
        ),
        Index(
            "ix_category_coefficient_current_lookup",
            "category",
            "effective_from",
            "effective_to",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    coefficient: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PayrollDeductionCategory(Base):
    __tablename__ = "payroll_deduction_category"
    __table_args__ = (
        CheckConstraint(
            "type in ('fine', 'withholding', 'deposit_writeoff')",
            name="ck_payroll_deduction_category_type",
        ),
        CheckConstraint(
            "default_amount is null or default_amount >= 0",
            name="ck_payroll_deduction_category_default_amount_non_negative",
        ),
        CheckConstraint(
            "effective_to is null or effective_to > effective_from",
            name="ck_payroll_deduction_category_effective_range",
        ),
        UniqueConstraint(
            "code",
            "effective_from",
            name="uq_payroll_deduction_category_code_effective_from",
        ),
        Index(
            "ix_payroll_deduction_category_current_lookup",
            "code",
            "effective_from",
            "effective_to",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(96), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    default_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PayrollAdjustmentCategory(Base):
    __tablename__ = "payroll_adjustment_category"
    __table_args__ = (
        CheckConstraint(
            "type in ('bonus', 'penalty')",
            name="ck_payroll_adjustment_category_type",
        ),
        CheckConstraint(
            "default_amount is null or default_amount >= 0",
            name="ck_payroll_adjustment_category_default_amount_non_negative",
        ),
        UniqueConstraint("code", name="uq_payroll_adjustment_category_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    code: Mapped[str] = mapped_column(String(96), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    sort_order: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class PayrollAdjustment(Base):
    __tablename__ = "payroll_adjustment"
    __table_args__ = (
        CheckConstraint(
            "type in ('bonus', 'penalty')",
            name="ck_payroll_adjustment_type",
        ),
        CheckConstraint("amount > 0", name="ck_payroll_adjustment_amount_positive"),
        CheckConstraint(
            "category_id is not null or custom_label is not null",
            name="ck_payroll_adjustment_category_or_custom",
        ),
        Index("ix_payroll_adjustment_employee_date", "employee_id", "work_date"),
        Index("ix_payroll_adjustment_work_date", "work_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="RESTRICT"), nullable=False
    )
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payroll_adjustment_category.id", ondelete="SET NULL"),
        nullable=True,
    )
    custom_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_by_label: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    category: Mapped[PayrollAdjustmentCategory | None] = relationship()


class DeferredAuditCharge(Base):
    __tablename__ = "deferred_audit_charge"
    __table_args__ = (
        CheckConstraint(
            "total_penalty_amount > 0",
            name="ck_deferred_charge_total_positive",
        ),
        CheckConstraint("splits_count > 0", name="ck_deferred_charge_splits_positive"),
        CheckConstraint(
            "status in ('pending','partially_applied','applied','cancelled')",
            name="ck_deferred_charge_status",
        ),
        CheckConstraint(
            "allocation_group in ('chefs','admins','common')",
            name="ck_deferred_charge_allocation_group",
        ),
        Index("ix_deferred_charge_audit", "source_audit_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_audit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inventory_audit.id", ondelete="RESTRICT"), nullable=False
    )
    source_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("inventory_audit_item.id", ondelete="SET NULL"), nullable=True
    )
    allocation_group: Mapped[str] = mapped_column(String(16), nullable=False)
    total_penalty_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    splits_count: Mapped[int] = mapped_column(nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    source_audit: Mapped[Any] = relationship("InventoryAudit")
    source_item: Mapped[Any | None] = relationship("InventoryAuditItem")
    created_by: Mapped[Any | None] = relationship(
        "User",
        lazy="raise",
        foreign_keys=[created_by_user_id],
    )
    recipients: Mapped[list[DeferredAuditChargeRecipient]] = relationship(
        back_populates="charge",
        cascade="all, delete-orphan",
        order_by=lambda: DeferredAuditChargeRecipient.created_at,
    )


class DeferredAuditChargeRecipient(Base):
    __tablename__ = "deferred_audit_charge_recipient"
    __table_args__ = (
        UniqueConstraint("charge_id", "employee_id", name="uq_deferred_charge_recipient"),
        CheckConstraint(
            "per_split_amount >= 0",
            name="ck_deferred_charge_recipient_amount_non_negative",
        ),
        Index("ix_deferred_charge_recipient_employee", "employee_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    charge_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deferred_audit_charge.id", ondelete="CASCADE"), nullable=False
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="RESTRICT"), nullable=False
    )
    per_split_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    splits_remaining: Mapped[int] = mapped_column(nullable=False)
    collapsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    collapse_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payroll_run.id", ondelete="SET NULL"), nullable=True
    )
    collapse_adjustment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payroll_adjustment.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    charge: Mapped[DeferredAuditCharge] = relationship(back_populates="recipients")
    employee: Mapped[Any] = relationship("Employee", lazy="raise")
    collapse_run: Mapped[PayrollRun | None] = relationship(
        foreign_keys=[collapse_run_id],
    )
    collapse_adjustment: Mapped[PayrollAdjustment | None] = relationship(
        foreign_keys=[collapse_adjustment_id],
    )
    splits: Mapped[list[DeferredAuditChargeSplit]] = relationship(
        back_populates="recipient",
        cascade="all, delete-orphan",
        order_by=lambda: DeferredAuditChargeSplit.split_index,
    )


class DeferredAuditChargeSplit(Base):
    __tablename__ = "deferred_audit_charge_split"
    __table_args__ = (
        UniqueConstraint(
            "recipient_id", "split_index", name="uq_deferred_charge_split_recipient_index"
        ),
        Index("ix_deferred_charge_split_recipient", "recipient_id"),
        Index("ix_deferred_charge_split_run", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deferred_audit_charge_recipient.id", ondelete="CASCADE"), nullable=False
    )
    split_index: Mapped[int] = mapped_column(nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payroll_run.id", ondelete="SET NULL"), nullable=True
    )
    adjustment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payroll_adjustment.id", ondelete="SET NULL"), nullable=True
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    recipient: Mapped[DeferredAuditChargeRecipient] = relationship(back_populates="splits")
    run: Mapped[PayrollRun | None] = relationship()
    adjustment: Mapped[PayrollAdjustment | None] = relationship()


class PayrollSeniorityPremium(Base):
    __tablename__ = "payroll_seniority_premium"
    __table_args__ = (
        CheckConstraint(
            "role in ('senior', 'deputy_senior')",
            name="ck_payroll_seniority_premium_role",
        ),
        CheckConstraint(
            "percent_of_base >= 0",
            name="ck_payroll_seniority_premium_percent_non_negative",
        ),
        CheckConstraint(
            "position in ('Повар', 'Кассир')",
            name="ck_payroll_seniority_premium_position",
        ),
        CheckConstraint(
            "amount >= 0",
            name="ck_payroll_seniority_premium_amount_non_negative",
        ),
        CheckConstraint(
            "effective_to is null or effective_to > effective_from",
            name="ck_payroll_seniority_premium_effective_range",
        ),
        UniqueConstraint(
            "position",
            "role",
            "effective_from",
            name="uq_seniority_premium_position_role_effective_from",
        ),
        Index(
            "ix_payroll_seniority_premium_current_lookup",
            "position",
            "role",
            "effective_from",
            "effective_to",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    position: Mapped[str] = mapped_column(String(160), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    percent_of_base: Mapped[Decimal | None] = mapped_column(Numeric(8, 5), nullable=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

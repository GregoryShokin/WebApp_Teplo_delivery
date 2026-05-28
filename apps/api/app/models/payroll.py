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
from sqlalchemy.orm import Mapped, mapped_column

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
    fund_accrual: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    deduction: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    total_payable: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    components: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )


class DepositAccount(Base):
    __tablename__ = "deposit_account"
    __table_args__ = (UniqueConstraint("employee_id", name="uq_deposit_account_employee"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="RESTRICT"), nullable=False
    )
    balance: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DepositTransaction(Base):
    __tablename__ = "deposit_transaction"
    __table_args__ = (
        CheckConstraint(
            "transaction_type in ('accrual', 'payout', 'write_off')",
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
        UniqueConstraint("employee_id", "year", name="uq_accumulation_fund_employee_year"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="RESTRICT"), nullable=False
    )
    year: Mapped[int] = mapped_column(nullable=False)
    accumulated_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    paid_out_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class PayrollRate(Base):
    __tablename__ = "payroll_rate"
    __table_args__ = (
        CheckConstraint(
            "rate_type in ('daily', 'hourly', 'monthly')",
            name="ck_payroll_rate_rate_type",
        ),
        CheckConstraint(
            "category in ('category_1', 'category_2', 'category_3', 'intern', 'freelancer')",
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
            "category in ('category_1', 'category_2', 'category_3', 'intern', 'freelancer')",
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
            "effective_to is null or effective_to > effective_from",
            name="ck_payroll_seniority_premium_effective_range",
        ),
        UniqueConstraint(
            "role",
            "effective_from",
            name="uq_payroll_seniority_premium_role_effective_from",
        ),
        Index(
            "ix_payroll_seniority_premium_current_lookup",
            "role",
            "effective_from",
            "effective_to",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    percent_of_base: Mapped[Decimal] = mapped_column(Numeric(8, 5), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

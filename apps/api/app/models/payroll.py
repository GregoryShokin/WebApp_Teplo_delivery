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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class PayrollPeriod(Base):
    __tablename__ = "payroll_period"
    __table_args__ = (
        CheckConstraint(
            "period_type in ('week', 'half_month')",
            name="ck_payroll_period_type",
        ),
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
    __table_args__ = (
        Index("ix_payroll_run_period_started", "period_id", "started_at"),
        CheckConstraint("payout_cash_total >= 0", name="ck_payroll_run_payout_cash_nonneg"),
    )

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
    # Total cash portion of the run's payroll, set at run level by the owner.
    # The bank draft (account-side payout) equals total payable minus this amount.
    payout_cash_total: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0, server_default="0"
    )
    # Наличный кошелёк, с которого выдаётся наличная часть (Сейф / Торговая касса
    # Черникова). Нужен для проводок ДДС наличной части админ-ведомости; NULL —
    # если наличной части нет.
    payout_cash_wallet_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("wallet.id", ondelete="RESTRICT"), nullable=True
    )


class PayrollRunEvent(Base):
    __tablename__ = "payroll_run_event"
    __table_args__ = (
        Index("ix_payroll_run_event_run_created_at", "run_id", "created_at"),
        Index("ix_payroll_run_event_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payroll_run.id", ondelete="SET NULL"), nullable=True
    )
    period_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payroll_period.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PayrollPayment(Base):
    __tablename__ = "payroll_payment"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_payroll_payment_amount_non_negative"),
        CheckConstraint("amount_cash >= 0", name="ck_payroll_payment_amount_cash_non_negative"),
        CheckConstraint(
            "amount_account >= 0",
            name="ck_payroll_payment_amount_account_non_negative",
        ),
        CheckConstraint(
            "overpaid_amount >= 0",
            name="ck_payroll_payment_overpaid_amount_non_negative",
        ),
        CheckConstraint(
            "status in ('planned', 'draft_created', 'paid')",
            name="ck_payroll_payment_status",
        ),
        CheckConstraint(
            "method is null or method in ('business_card', 'cash', 'transfer', 'other')",
            name="ck_payroll_payment_method",
        ),
        UniqueConstraint("run_id", "employee_id", name="uq_payroll_payment_run_employee"),
        Index("ix_payroll_payment_run_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payroll_run.id", ondelete="CASCADE"), nullable=False
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="RESTRICT"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    amount_cash: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0, server_default="0"
    )
    amount_account: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0, server_default="0"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="paid", server_default="paid"
    )
    draft_document_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    draft_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    draft_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    overpaid_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0, server_default="0"
    )
    paid_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    paid_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PayrollBankDraft(Base):
    __tablename__ = "payroll_bank_draft"
    __table_args__ = (
        CheckConstraint(
            "status in ('created', 'updated', 'paid', 'failed')",
            name="ck_payroll_bank_draft_status",
        ),
        UniqueConstraint("run_id", name="uq_payroll_bank_draft_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payroll_run.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    provider_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
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
    advance_recovered: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0, server_default="0"
    )
    deposit_excluded_for_run: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    deposit_exclusion_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Запланированная выдача депозита, попавшая в эту ведомость (столбец «Выдача депозита»).
    # В total_payable НЕ входит — выплачивается отдельно через Сейф-контур (этап 4–5).
    deposit_payout_scheduled: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0, server_default="0"
    )
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


class DepositPayoutSchedule(Base):
    """Намерение выдать депозит сотруднику в ближайшей ЗП-ведомости (отложенная выдача).

    Переживает пересчёт ведомости (в отличие от превью ``deposit_transaction``). При расчёте
    pending-план попадает в столбец «Выдача депозита» строки и выплачивается через Сейф-контур;
    при финализации становится ``processed`` (``target_run_id`` = прогон выдачи), при откате —
    обратно ``pending``. Один pending на сотрудника (частичный уникальный индекс).
    """

    __tablename__ = "deposit_payout_schedule"
    __table_args__ = (
        CheckConstraint(
            "status in ('pending', 'processed', 'cancelled')",
            name="ck_deposit_payout_schedule_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="CASCADE"), nullable=False
    )
    target_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payroll_run.id", ondelete="SET NULL"), nullable=True
    )
    requested_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    account_choice: Mapped[str] = mapped_column(
        String(32), nullable=False, default="safe", server_default="safe"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
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
            "('category_1', 'category_2', 'category_3', 'category_4', "
            "'intern', 'freelancer', 'admin')",
            name="ck_payroll_rate_category_value",
        ),
        CheckConstraint("amount >= 0", name="ck_payroll_rate_amount_non_negative"),
        CheckConstraint(
            "(is_active = false) OR (amount IS NOT NULL)",
            name="ck_payroll_rate_active_amount",
        ),
        CheckConstraint(
            "effective_to is null or effective_to > effective_from",
            name="ck_payroll_rate_effective_range",
        ),
        # Натуральный ключ расщеплён: дефолты по должности (employee_id IS NULL) и
        # переопределения окладов на конкретного сотрудника (employee_id указан) не
        # конфликтуют между собой.
        Index(
            "uq_payroll_rate_natural_default",
            "position_group",
            "category",
            "station",
            "rate_type",
            "effective_from",
            unique=True,
            postgresql_where=text("employee_id IS NULL"),
        ),
        Index(
            "uq_payroll_rate_natural_employee",
            "employee_id",
            "position_group",
            "category",
            "station",
            "rate_type",
            "effective_from",
            unique=True,
            postgresql_where=text("employee_id IS NOT NULL"),
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
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employee.id", ondelete="CASCADE"), nullable=True
    )
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
        Index("ix_payroll_adjustment_role_date", "role", "work_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="RESTRICT"), nullable=False
    )
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Роль, к которой относится начисление (разводит deньги по производственной и
    # админской ведомостям для сотрудников с несколькими ролями).
    role: Mapped[str] = mapped_column(String(160), nullable=False, server_default="Повар")
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
    # Стартовая выплата распределения: период (вторник…понедельник), в который садится
    # первая доля; последующие доли идут подряд по неделям. None = legacy-поведение
    # (доли применяются в ближайших прогонах без привязки к дате).
    start_period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
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


class DishwasherShift(Base):
    """Смена мойщицы, проставленная управляющим вручную (без iiko).

    Оплата мойщиц посменная: пул ₽/мес ÷ календарные дни месяца = ставка за смену,
    сумма каждой = (её смены в полупериоде) × ставку. Считается в полумесячной
    окладной ведомости рядом с администрацией.
    """

    __tablename__ = "dishwasher_shift"
    __table_args__ = (
        UniqueConstraint("employee_id", "work_date", name="uq_dishwasher_shift_employee_date"),
        Index("ix_dishwasher_shift_work_date", "work_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="CASCADE"), nullable=False
    )
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SalaryAdvance(Base):
    """Аванс или заём сотруднику payroll-контура.

    Аванс — выдача в пределах уже заработанного на дату (право A), гасится разом
    в ближайшей ведомости. Заём — сверх заработанного (право B), гасится в
    рассрочку равными долями за `installments_count` периодов. Это источник
    истины об авансах: выдача регистрируется как факт выплаты для ДДС, возврат
    нетится в `PayrollLine.deduction`. Дискриминатор `role` разводит возврат по
    производственной/админской ведомости (как `PayrollAdjustment.role`).

    Аванс: `kind='advance'`, `installments_count=1`, `per_installment_amount=amount`.
    Заём: `kind='loan'`, `installments_count=N`, `per_installment_amount≈amount/N`.

    Заём можно выдать и в пределах заработанного (явный выбор типа), и отложить
    начало удержания через `recovery_start_date` (NULL = с ближайшей ведомости).
    """

    __tablename__ = "salary_advance"
    __table_args__ = (
        CheckConstraint("kind in ('advance', 'loan')", name="ck_salary_advance_kind"),
        CheckConstraint("amount > 0", name="ck_salary_advance_amount_positive"),
        CheckConstraint(
            "per_installment_amount > 0",
            name="ck_salary_advance_per_installment_positive",
        ),
        CheckConstraint(
            "installments_count >= 1", name="ck_salary_advance_installments_positive"
        ),
        CheckConstraint(
            "recovered_amount >= 0", name="ck_salary_advance_recovered_non_negative"
        ),
        CheckConstraint(
            "recovered_amount <= amount", name="ck_salary_advance_recovered_le_amount"
        ),
        CheckConstraint(
            "status in ('issued', 'awaiting_payout', 'recovered', 'written_off', 'cancelled')",
            name="ck_salary_advance_status",
        ),
        CheckConstraint(
            "payout_method is null or payout_method in "
            "('business_card', 'cash', 'transfer', 'other')",
            name="ck_salary_advance_payout_method",
        ),
        Index("ix_salary_advance_employee_status", "employee_id", "status"),
        Index("ix_salary_advance_role_status", "role", "status"),
        Index("ix_salary_advance_issued_on", "issued_on"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="advance")
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    per_installment_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    installments_count: Mapped[int] = mapped_column(
        nullable=False, default=1, server_default="1"
    )
    recovered_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0, server_default="0"
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="issued", server_default="issued"
    )
    issued_on: Mapped[date] = mapped_column(Date, nullable=False)
    # С какой даты заём попадает в удержание. NULL = с ближайшей ведомости.
    recovery_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    payout_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Счёт-источник выдачи (ТК Черникова / Сейф / банк) — определяет проводку ДДС.
    wallet_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("wallet.id", ondelete="SET NULL"), nullable=True
    )
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

    recoveries: Mapped[list[SalaryAdvanceRecovery]] = relationship(
        back_populates="advance",
        cascade="all, delete-orphan",
        order_by=lambda: SalaryAdvanceRecovery.created_at,
    )


class SalaryAdvanceRecovery(Base):
    """Факт удержания части аванса/займа в конкретной ведомости.

    Создаётся при расчёте ведомости: гасит `min(остаток, доля, доступный net)`,
    чтобы выплата не ушла в минус (anti-negative); недобор переносится на
    следующую ведомость. История возврата — аудит и источник строки возврата.
    """

    __tablename__ = "salary_advance_recovery"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_salary_advance_recovery_amount_positive"),
        Index("ix_salary_advance_recovery_advance", "advance_id"),
        Index("ix_salary_advance_recovery_run", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    advance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("salary_advance.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payroll_run.id", ondelete="SET NULL"), nullable=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    advance: Mapped[SalaryAdvance] = relationship(back_populates="recoveries")


class SalaryAdvanceRecoveryOverride(Base):
    """Ручное переопределение суммы удержания аванса/займа в конкретном периоде.

    Окно «Удержания сотрудника»: вместо дефолтной доли (`per_installment_amount`
    для займа, вся сумма для аванса) при расчёте ведомости этого периода удерживается
    заданная сумма — ``0`` откладывает удержание, ``= остатку долга`` закрывает досрочно,
    между — частичное гашение. Ключ (advance_id, period_id) переживает пересчёт:
    превью-строки `salary_advance_recovery` удаляются на ре-расчёте, override — нет.
    """

    __tablename__ = "salary_advance_recovery_override"
    __table_args__ = (
        UniqueConstraint("advance_id", "period_id", name="uq_advance_recovery_override"),
        CheckConstraint(
            "amount >= 0", name="ck_advance_recovery_override_amount_non_negative"
        ),
        Index("ix_advance_recovery_override_period", "period_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    advance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("salary_advance.id", ondelete="CASCADE"), nullable=False
    )
    period_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payroll_period.id", ondelete="CASCADE"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SalaryAdvanceBankDraft(Base):
    """Банковский черновик выдачи аванса/займа (Фаза 2) — зеркало ``PayrollBankDraft``.

    Когда счёт-источник выдачи — банковский кошелёк, деньги не уходят мгновенно:
    создаётся платёжный черновик в Т-Банке, поллинг (``run_payment_status_poll``)
    ловит статус «исполнен» по ``provider_ref`` (documentId), тогда заводится транзит
    банк→Сейф и резерв Сейфа (``safe_allocation_id``) со статьёй аванса/займа.
    Фактическая выдача сотруднику — оплата резерва по кнопке «Выплачено», после чего
    черновик → ``disbursed``, а ``SalaryAdvance`` → ``issued`` (формируется долг).

    status: ``created``/``updated`` (в банке) → ``paid`` (исполнен, деньги в Сейфе как
    резерв) → ``disbursed`` (выдан сотруднику); ``failed`` — отклонён банком;
    ``cancelled`` — отменён до выдачи.
    """

    __tablename__ = "salary_advance_bank_draft"
    __table_args__ = (
        CheckConstraint(
            "status in ('created', 'updated', 'paid', 'disbursed', 'failed', 'cancelled')",
            name="ck_salary_advance_bank_draft_status",
        ),
        UniqueConstraint("advance_id", name="uq_salary_advance_bank_draft_advance_id"),
        Index("ix_salary_advance_bank_draft_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    advance_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("salary_advance.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    provider_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Резерв Сейфа, созданный при исполнении платежа (источник кнопки «Выплачено»).
    safe_allocation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("safe_allocations.id", ondelete="SET NULL"), nullable=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EmployeePayout(Base):
    """Разовая выплата сотруднику вне обычной ведомости (леджер «выплачено»).

    Общий леджер двух сценариев: (1) ЗП собственника «по востребованию» — оклад в режиме
    ``on_demand`` начисляется в долг с 1-го числа, а гасится произвольными суммами по кнопке
    «Включить в выплату»; (2) разовая «Выплата сотруднику» из плавающей кнопки. Создаётся
    реальным расходом денег (out-``CashflowTransaction`` со статьёй ДДС,
    ``source_kind='employee_payout'``, ``source_id`` = id этого payout'а).

    Долг собственника = Σ начислено (``accrual_amount`` on_demand-строк проведённых
    ведомостей — по одной на период) − Σ ``amount`` его payout'ов. Наличный источник гасит сразу
    (``status='paid'``); банковский источник (T-Bank/ИП) в Треке B заведёт черновик + транзит
    на Сейф с резервом (доп. поля появятся в B2), до исполнения ``status='pending'``.
    """

    __tablename__ = "employee_payout"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_employee_payout_amount_positive"),
        CheckConstraint(
            "status in ('draft', 'pending', 'included', 'paid', 'failed', 'cancelled')",
            name="ck_employee_payout_status",
        ),
        Index("ix_employee_payout_employee", "employee_id"),
        Index("ix_employee_payout_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="RESTRICT"), nullable=False
    )
    # owner_salary — ЗП собственника по востребованию; salary — разовая выплата; other.
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="owner_salary", server_default="owner_salary"
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    payout_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Источник денег: наличные/банк/Сейф. Nullable, пока не выбран (черновик Трека B).
    wallet_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("wallet.id", ondelete="SET NULL"), nullable=True
    )
    article_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dds_articles.id", ondelete="SET NULL"), nullable=True
    )
    # Денежный факт (out-проводка), породивший выплату.
    cashflow_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("cashflow_transactions.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="paid", server_default="paid"
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # --- Банковский контур (Track B2/B3): состояние черновика Т-Банк + Сейф-резерв + привязка ---
    # Наличная/сейфовая выплата (status='paid' сразу) эти поля не использует.
    document_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Резерв Сейфа, созданный при исполнении банковской выплаты (источник «Выплачено»).
    safe_allocation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("safe_allocations.id", ondelete="SET NULL"), nullable=True
    )
    # Привязанная банковская операция из выписки (ручное подтверждение выплаты).
    bank_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("bank_operations.id", ondelete="SET NULL"), nullable=True
    )
    # Ведомость, в выплату которой включена сумма (status='included', «Включить в выплату»);
    # эти суммы формируют total_payable on_demand-строки и платятся вместе с ведомостью.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payroll_run.id", ondelete="SET NULL"), nullable=True
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy import UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class PayrollForecastRun(Base):
    __tablename__ = "payroll_forecast_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','completed','superseded')",
            name="status",
        ),
        Index("ix_payroll_forecast_run_schedule_run_at", "shift_schedule_id", "run_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shift_schedule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shift_schedule.id", ondelete="CASCADE"), nullable=False
    )
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    run_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    rule_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    total_revenue_forecast: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    total_shift_cost_estimate: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), nullable=True
    )
    fot_to_revenue_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 5), nullable=True)
    shifts_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shifts_with_warnings: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    estimates: Mapped[list[ShiftCostEstimate]] = relationship(
        back_populates="forecast_run",
        cascade="all, delete-orphan",
        order_by=lambda: (ShiftCostEstimate.business_date, ShiftCostEstimate.employee_id),
    )


class ShiftCostEstimate(Base):
    __tablename__ = "shift_cost_estimate"
    __table_args__ = (
        CheckConstraint(
            "quality_status IN ('ok','requires_review')",
            name="quality_status",
        ),
        UniqueConstraint(
            "forecast_run_id",
            "scheduled_shift_id",
            name="uq_shift_cost_estimate_run_shift",
        ),
        Index("ix_shift_cost_estimate_run", "forecast_run_id"),
        Index("ix_shift_cost_estimate_employee_date", "employee_id", "business_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    forecast_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payroll_forecast_run.id", ondelete="CASCADE"), nullable=False
    )
    scheduled_shift_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scheduled_shift.id", ondelete="CASCADE"), nullable=False
    )
    revenue_forecast_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("revenue_forecast.id", ondelete="SET NULL"), nullable=True
    )
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="RESTRICT"), nullable=False
    )
    planned_hours: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    base_salary_estimate: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    weekday_premium_estimate: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    allowance_estimate: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    revenue_percent_estimate: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    fund_accrual_estimate: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    total_cost_estimate: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    quality_status: Mapped[str] = mapped_column(String(24), nullable=False)
    quality_reasons: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    breakdown: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    forecast_run: Mapped[PayrollForecastRun] = relationship(back_populates="estimates")
    employee: Mapped[Any] = relationship("Employee")
    scheduled_shift: Mapped[Any] = relationship("ScheduledShift")

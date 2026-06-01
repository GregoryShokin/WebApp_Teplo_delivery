from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ShiftSchedule(Base):
    __tablename__ = "shift_schedule"
    __table_args__ = (
        CheckConstraint(
            "status in ('draft', 'published', 'superseded')",
            name="status",
        ),
        CheckConstraint("date_end >= date_start", name="date_range"),
        Index("ix_shift_schedule_status_date_start", "status", "date_start"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    date_start: Mapped[date] = mapped_column(Date, nullable=False)
    date_end: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("shift_schedule.id", ondelete="SET NULL"), nullable=True
    )

    shifts: Mapped[list[ScheduledShift]] = relationship(
        back_populates="schedule",
        cascade="all, delete-orphan",
        order_by=lambda: (ScheduledShift.business_date, ScheduledShift.employee_id),
    )
    cashier_allowance_overrides: Mapped[list[ShiftAllowanceOverride]] = relationship(
        back_populates="schedule",
        cascade="all, delete-orphan",
        order_by=lambda: ShiftAllowanceOverride.business_date,
    )


class ScheduledShift(Base):
    __tablename__ = "scheduled_shift"
    __table_args__ = (
        CheckConstraint(
            "planned_end_at > planned_start_at",
            name="interval",
        ),
        UniqueConstraint(
            "shift_schedule_id",
            "business_date",
            "employee_id",
            name="uq_scheduled_shift_schedule_date_employee",
        ),
        Index("ix_scheduled_shift_schedule_date", "shift_schedule_id", "business_date"),
        Index("ix_scheduled_shift_employee_date", "employee_id", "business_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shift_schedule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shift_schedule.id", ondelete="CASCADE"), nullable=False
    )
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="RESTRICT"), nullable=False
    )
    payroll_role: Mapped[str] = mapped_column(String(64), nullable=False)
    station_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    planned_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    planned_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    comment_private: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    schedule: Mapped[ShiftSchedule] = relationship(back_populates="shifts")


class ShiftAllowanceOverride(Base):
    __tablename__ = "shift_allowance_override"
    __table_args__ = (
        CheckConstraint(
            "recipient_role IN ('senior', 'deputy_senior', 'none')",
            name="recipient_role",
        ),
        CheckConstraint("position = 'Кассир'", name="position_cashier_only"),
        CheckConstraint(
            "(recipient_role = 'none' AND recipient_employee_id IS NULL) OR "
            "(recipient_role IN ('senior','deputy_senior') "
            "AND recipient_employee_id IS NOT NULL)",
            name="recipient_consistency",
        ),
        UniqueConstraint(
            "shift_schedule_id",
            "business_date",
            "position",
            name="uq_shift_allowance_override_schedule_date_position",
        ),
        Index("ix_shift_allowance_override_date_position", "business_date", "position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shift_schedule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("shift_schedule.id", ondelete="CASCADE"), nullable=False
    )
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    position: Mapped[str] = mapped_column(String(160), nullable=False, default="Кассир")
    recipient_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employee.id", ondelete="SET NULL"), nullable=True
    )
    recipient_role: Mapped[str] = mapped_column(String(16), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    set_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    set_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    schedule: Mapped[ShiftSchedule] = relationship(back_populates="cashier_allowance_overrides")

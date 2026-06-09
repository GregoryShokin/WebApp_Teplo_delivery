from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CourierScheduleEntry(Base):
    __tablename__ = "courier_schedule_entry"
    __table_args__ = (
        CheckConstraint(
            "planned_end_at > planned_start_at",
            name="ck_courier_schedule_entry_time_range",
        ),
        CheckConstraint(
            "category in ('primary', 'secondary')",
            name="ck_courier_schedule_entry_category",
        ),
        UniqueConstraint(
            "courier_employee_id",
            "work_date",
            name="uq_courier_schedule_entry_courier_work_date",
        ),
        Index("ix_courier_schedule_entry_work_date_category", "work_date", "category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    courier_employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employee.id", ondelete="RESTRICT"),
        nullable=False,
    )
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False, default="secondary")
    planned_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    planned_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

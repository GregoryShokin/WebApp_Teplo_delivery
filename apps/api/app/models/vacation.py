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
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class VacationPeriod(Base):
    __tablename__ = "vacation_period"
    __table_args__ = (
        CheckConstraint("date_end >= date_start", name="date_range"),
        CheckConstraint(
            "status IN ('planned', 'paid', 'cancelled')",
            name="status",
        ),
        CheckConstraint("days_count > 0", name="days_positive"),
        CheckConstraint(
            "EXTRACT(YEAR FROM date_start) = EXTRACT(YEAR FROM date_end)",
            name="same_year",
        ),
        Index("ix_vacation_period_employee_start", "employee_id", "date_start"),
        Index("ix_vacation_period_date_range", "date_start", "date_end"),
        Index("ix_vacation_period_payout_date", "payout_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="RESTRICT"), nullable=False
    )
    date_start: Mapped[date] = mapped_column(Date, nullable=False)
    date_end: Mapped[date] = mapped_column(Date, nullable=False)
    days_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Дата выплаты отпускных одним траншем (вторник = payroll_date недельной
    # ведомости). NULL — легаси: отпускные размазываются по дням, как раньше.
    payout_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="planned")
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    employee = relationship("Employee")
    created_by = relationship("User")

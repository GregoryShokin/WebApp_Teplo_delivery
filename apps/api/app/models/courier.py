from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import (
    Enum as SQLEnum,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CourierShiftMatchStatus(StrEnum):
    MATCHED_PRIMARY = "matched_primary"
    SHORT_PRIMARY = "short_primary"
    NO_SHOW_PRIMARY = "no_show_primary"
    MATCHED_SECONDARY = "matched_secondary"
    SHORT_SECONDARY = "short_secondary"
    NO_SHOW_SECONDARY = "no_show_secondary"
    HELPING = "helping"
    NOT_COUNTED = "not_counted"


courier_shift_match_status_enum = SQLEnum(
    CourierShiftMatchStatus,
    name="courier_shift_match_status",
    values_callable=lambda values: [item.value for item in values],
)


class DeliveryOrder(Base):
    __tablename__ = "delivery_order"
    __table_args__ = (
        Index("ix_delivery_order_work_date", "work_date"),
        Index("ix_delivery_order_courier_work_date", "courier_iiko_id", "work_date"),
        Index("ix_delivery_order_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    iiko_order_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    order_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    service_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    courier_iiko_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    on_way_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    way_duration_minutes: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    revenue: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    raw: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CourierIikoShift(Base):
    __tablename__ = "courier_iiko_shift"
    __table_args__ = (
        UniqueConstraint(
            "iiko_employee_id",
            "opened_at",
            name="uq_courier_iiko_shift_iiko_employee_opened",
        ),
        Index("ix_courier_iiko_shift_employee_opened", "employee_id", "opened_at"),
        Index("ix_courier_iiko_shift_opened_at", "opened_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    iiko_employee_id: Mapped[str] = mapped_column(String(128), nullable=False)
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employee.id", ondelete="SET NULL"),
        nullable=True,
    )
    iiko_role_id: Mapped[str] = mapped_column(String(128), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attendance_type: Mapped[str] = mapped_column(String(8), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class CourierShiftMatch(Base):
    __tablename__ = "courier_shift_match"
    __table_args__ = (
        UniqueConstraint(
            "courier_employee_id",
            "work_date",
            "schedule_entry_id",
            "iiko_shift_id",
            name="uq_courier_shift_match_courier_day_plan_fact",
        ),
        Index("ix_courier_shift_match_courier_date", "courier_employee_id", "work_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    courier_employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employee.id", ondelete="RESTRICT"),
        nullable=False,
    )
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    schedule_entry_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("courier_schedule_entry.id", ondelete="SET NULL"),
        nullable=True,
    )
    iiko_shift_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("courier_iiko_shift.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[CourierShiftMatchStatus] = mapped_column(
        courier_shift_match_status_enum,
        nullable=False,
    )
    late_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    worked_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deliveries_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recalculated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, DateTime, Index, Numeric, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


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
    way_duration_minutes: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
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

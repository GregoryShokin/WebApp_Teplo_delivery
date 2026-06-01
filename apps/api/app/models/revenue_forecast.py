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
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RevenueForecast(Base):
    __tablename__ = "revenue_forecast"
    __table_args__ = (
        UniqueConstraint("business_date", name="uq_revenue_forecast_business_date"),
        CheckConstraint(
            "quality_status IN ('ok','requires_review','manual_override')",
            name="quality_status",
        ),
        CheckConstraint("weekday >= 0 AND weekday <= 6", name="weekday"),
        Index("ix_revenue_forecast_business_date", "business_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    weekday: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    method_code: Mapped[str] = mapped_column(
        String(48),
        nullable=False,
        default="avg_6_same_weekday",
        server_default=text("'avg_6_same_weekday'"),
    )
    history_window_weeks: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=6,
        server_default=text("6"),
    )
    history_points: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    base_average_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    season_coeff: Mapped[Decimal] = mapped_column(
        Numeric(8, 5),
        nullable=False,
        default=Decimal("1.0"),
        server_default=text("1.0"),
    )
    event_coeff: Mapped[Decimal] = mapped_column(
        Numeric(8, 5),
        nullable=False,
        default=Decimal("1.0"),
        server_default=text("1.0"),
    )
    manual_override_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    manual_override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_override_set_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    manual_override_set_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    forecast_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    quality_status: Mapped[str] = mapped_column(String(24), nullable=False)
    event_review_recommended: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

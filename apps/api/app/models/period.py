from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import period_status_enum, period_type_enum


class Period(Base):
    __tablename__ = "period"
    __table_args__ = (
        Index("uq_period_type_start_end", "period_type", "start_date", "end_date", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    period_type: Mapped[str] = mapped_column(period_type_enum, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        period_status_enum, nullable=False, default="open", server_default="open"
    )

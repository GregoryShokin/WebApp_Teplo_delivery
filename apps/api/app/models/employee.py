from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Employee(Base):
    __tablename__ = "employee"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    full_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="source=iiko; read-only in app",
        info={"source": "iiko", "read_only": True},
    )
    iiko_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    position: Mapped[str | None] = mapped_column(
        String(160), nullable=True, comment="source=app_managed"
    )
    category: Mapped[str | None] = mapped_column(
        String(160), nullable=True, comment="source=app_managed"
    )
    status: Mapped[str] = mapped_column(
        String(64), nullable=False, default="active", comment="source=app_managed"
    )
    hire_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="source=app_managed"
    )
    fire_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="source=app_managed"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

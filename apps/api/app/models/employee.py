from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import employee_status_enum


class Employee(Base):
    __tablename__ = "employee"
    __table_args__ = (
        CheckConstraint(
            "category is null or category in "
            "('category_1', 'category_2', 'category_3', 'intern', 'freelancer')",
            name="ck_employee_category_value",
        ),
        CheckConstraint(
            "default_cooking_station is null or default_cooking_station in "
            "('sushi', 'pizza', 'shawarma')",
            name="ck_employee_default_cooking_station_value",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    full_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="source=iiko; read-only in app",
        info={"source": "iiko", "read_only": True},
    )
    iiko_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    position: Mapped[str | None] = mapped_column(
        String(160), nullable=True, comment="source=iiko"
    )
    category: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="source=app_managed"
    )
    default_cooking_station: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="source=app_managed"
    )
    is_senior: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="source=app_managed",
    )
    is_deputy_senior: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="source=app_managed",
    )
    status: Mapped[str] = mapped_column(
        employee_status_enum,
        nullable=False,
        default="active",
        server_default="active",
        comment="source=app_managed",
    )
    hire_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="source=app_managed"
    )
    fire_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="source=app_managed"
    )
    iiko_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="source=iiko"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

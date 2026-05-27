from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import Date, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "organization"

    legal_name: Mapped[str] = mapped_column(String(255))
    tax_profile: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="active")


class AppRole(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "app_role"

    code: Mapped[str] = mapped_column(String(64), unique=True)
    permissions_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(64), default="active")


class AppUser(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "app_user"

    display_name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(64), default="active")


class Location(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "location"

    organization_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("organization.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(160))
    location_type: Mapped[str] = mapped_column(String(64), default="store")
    active_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    active_to: Mapped[date | None] = mapped_column(Date, nullable=True)


class Period(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "period"
    __table_args__ = (
        Index("ix_period_type_dates", "period_type", "date_start", "date_end", unique=True),
    )

    period_type: Mapped[str] = mapped_column(String(32))
    date_start: Mapped[date] = mapped_column(Date)
    date_end: Mapped[date] = mapped_column(Date)
    close_status: Mapped[str] = mapped_column(String(64), default="open")


class Counterparty(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "counterparty"

    working_name: Mapped[str] = mapped_column(String(255))
    legal_name_private: Mapped[str | None] = mapped_column(String(512), nullable=True)
    payment_roles_json: Mapped[list[str]] = mapped_column(JSONB, default=list)
    owner_status: Mapped[str] = mapped_column(String(64), default="unreviewed")
    source_status: Mapped[str] = mapped_column(String(64), default="imported")

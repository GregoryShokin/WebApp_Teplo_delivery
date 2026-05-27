from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AppSetting(Base):
    __tablename__ = "app_setting"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    value_type: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    widget_type: Mapped[str] = mapped_column(Text, nullable=False)
    widget_options: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    unit: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="RESTRICT"), nullable=True
    )


class AppSettingHistory(Base):
    __tablename__ = "app_setting_history"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    setting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_setting.id", ondelete="CASCADE"), nullable=False
    )
    old_value: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    changed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="RESTRICT"), nullable=True
    )

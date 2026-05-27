from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ManualAction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "manual_action"

    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    action_type: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SourceReference(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "source_reference"
    __table_args__ = (
        Index("ix_source_reference_entity", "module_code", "entity_type", "entity_id"),
    )

    module_code: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(128))
    entity_id: Mapped[UUID | None] = mapped_column(nullable=True)
    source_snapshot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("source_snapshot.id"), nullable=True
    )
    agent_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("agent_run.id"), nullable=True)
    manual_action_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("manual_action.id"), nullable=True
    )
    evidence_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    evidence_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quality_status: Mapped[str] = mapped_column(String(64), default="draft")


class AuditEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_event"
    __table_args__ = (
        Index("ix_audit_event_subject", "subject_type", "subject_id", "occurred_at"),
    )

    subject_type: Mapped[str] = mapped_column(String(128))
    subject_id: Mapped[UUID | None] = mapped_column(nullable=True)
    event_type: Mapped[str] = mapped_column(String(128))
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

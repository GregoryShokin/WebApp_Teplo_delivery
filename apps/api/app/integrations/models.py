from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Date, DateTime, ForeignKey, Index, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DataSource(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "data_source"

    name: Mapped[str] = mapped_column(String(160), unique=True)
    source_type: Mapped[str] = mapped_column(String(64))
    primary_pattern: Mapped[str] = mapped_column(String(64))
    business_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    coverage_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pii_level: Mapped[str] = mapped_column(String(32), default="medium")
    canonical_periodicity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_status: Mapped[str] = mapped_column(String(64), default="active")
    last_successful_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SourceCredential(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "source_credential"

    source_id: Mapped[UUID] = mapped_column(ForeignKey("data_source.id"))
    credential_type: Mapped[str] = mapped_column(String(64))
    secret_ref: Mapped[str] = mapped_column(String(512))
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="missing")
    rotation_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agent_run"
    __table_args__ = (
        Index(
            "ix_agent_run_source_period",
            "source_id",
            "requested_period_start",
            "requested_period_end",
        ),
    )

    source_id: Mapped[UUID] = mapped_column(ForeignKey("data_source.id"))
    pattern: Mapped[str] = mapped_column(String(64))
    agent_name: Mapped[str] = mapped_column(String(128))
    trigger_type: Mapped[str] = mapped_column(String(64))
    requested_period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    requested_period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="partial")
    code_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pii_access_level: Mapped[str] = mapped_column(String(32), default="medium")
    private_artifact_root: Mapped[str | None] = mapped_column(String(512), nullable=True)
    processed_artifact_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(128), nullable=True)


class AgentAction(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "agent_action"
    __table_args__ = (Index("ix_agent_action_run_sequence", "run_id", "sequence_no", unique=True),)

    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_run.id"))
    sequence_no: Mapped[int]
    action_type: Mapped[str] = mapped_column(String(64))
    target_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_ref_masked: Mapped[str | None] = mapped_column(String(512), nullable=True)
    method: Mapped[str | None] = mapped_column(String(16), nullable=True)
    selector_or_endpoint: Mapped[str | None] = mapped_column(String(512), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="partial")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    human_required: Mapped[bool] = mapped_column(default=False)
    error_message_masked: Mapped[str | None] = mapped_column(Text, nullable=True)


class ParsedDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "parsed_document"
    __table_args__ = (Index("ix_parsed_document_source_date", "source_id", "document_date"),)

    source_id: Mapped[UUID] = mapped_column(ForeignKey("data_source.id"))
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("agent_run.id"), nullable=True)
    raw_artifact_ref: Mapped[str] = mapped_column(String(512))
    raw_sha256: Mapped[str] = mapped_column(String(64))
    document_type: Mapped[str] = mapped_column(String(64))
    document_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    document_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    sender: Mapped[str | None] = mapped_column(String(255), nullable=True)
    counterparty_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("counterparty.id"), nullable=True
    )
    counterparty_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    inn: Mapped[str | None] = mapped_column(String(32), nullable=True)
    service_period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    service_period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    dds_article_candidate: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pnl_article_candidate: Mapped[str | None] = mapped_column(String(255), nullable=True)
    parser_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    recognition_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    quality_status: Mapped[str] = mapped_column(String(64), default="raw_synced")
    evidence_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    linked_bank_operation_id: Mapped[UUID | None] = mapped_column(nullable=True)
    linked_source_document_id: Mapped[UUID | None] = mapped_column(nullable=True)


class SourceSnapshot(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "source_snapshot"
    __table_args__ = (
        Index("ix_source_snapshot_source_period", "source_id", "period_start", "period_end"),
    )

    source_id: Mapped[UUID] = mapped_column(ForeignKey("data_source.id"))
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("agent_run.id"), nullable=True)
    snapshot_type: Mapped[str] = mapped_column(String(64))
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    private_raw_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    processed_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    row_count: Mapped[int | None] = mapped_column(nullable=True)
    schema_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quality_status: Mapped[str] = mapped_column(String(64), default="partial")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class CredentialEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "credential_event"

    credential_id: Mapped[UUID] = mapped_column(ForeignKey("source_credential.id"))
    event_type: Mapped[str] = mapped_column(String(64))
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    detected_by_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agent_run.id"), nullable=True
    )
    old_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_status: Mapped[str] = mapped_column(String(64))
    owner_action_required: Mapped[bool] = mapped_column(default=False)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)

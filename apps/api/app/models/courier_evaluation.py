from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CourierEvaluationSource(StrEnum):
    WEB = "web"
    TELEGRAM = "telegram"
    API = "api"


courier_evaluation_source_enum = SQLEnum(
    CourierEvaluationSource,
    name="courier_evaluation_source",
    values_callable=lambda values: [item.value for item in values],
)


class CourierEvaluationCriterion(Base):
    __tablename__ = "courier_evaluation_criterion"
    __table_args__ = (
        CheckConstraint(
            "score between -7 and 7",
            name="ck_courier_evaluation_criterion_score_range",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)


class CourierEvaluation(Base):
    __tablename__ = "courier_evaluation"
    __table_args__ = (
        CheckConstraint(
            "score_snapshot between -7 and 7",
            name="ck_courier_evaluation_score_snapshot_range",
        ),
        Index(
            "ix_courier_evaluation_courier_date",
            "courier_employee_id",
            "evaluated_at",
        ),
        Index("ix_courier_evaluation_author_created", "created_by", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    courier_employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employee.id", ondelete="RESTRICT"),
        nullable=False,
    )
    criterion_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("courier_evaluation_criterion.id", ondelete="RESTRICT"),
        nullable=False,
    )
    score_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluated_at: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        server_default=func.current_date(),
    )
    source: Mapped[CourierEvaluationSource] = mapped_column(
        courier_evaluation_source_enum,
        nullable=False,
        default=CourierEvaluationSource.WEB,
        server_default=CourierEvaluationSource.WEB.value,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employee.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

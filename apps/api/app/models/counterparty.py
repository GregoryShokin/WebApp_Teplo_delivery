from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import counterparty_role_enum, counterparty_type_enum


class Counterparty(Base):
    __tablename__ = "counterparty"
    __table_args__ = (
        Index(
            "uq_counterparty_inn_not_null",
            "inn",
            unique=True,
            postgresql_where=text("inn IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    inn: Mapped[str | None] = mapped_column(String(12), nullable=True)
    type: Mapped[str] = mapped_column(counterparty_type_enum, nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="active")
    # Откуда карточка появилась: iiko / manual / email / sbis (различение в реестре).
    origin: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CounterpartyRole(Base):
    __tablename__ = "counterparty_role"

    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("counterparty.id", ondelete="RESTRICT"), primary_key=True
    )
    role: Mapped[str] = mapped_column(counterparty_role_enum, primary_key=True)

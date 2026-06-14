from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Text, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CourierShiftDayStatus(StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"


courier_shift_day_status_enum = SQLEnum(
    CourierShiftDayStatus,
    name="courier_shift_day_status",
    values_callable=lambda values: [item.value for item in values],
)


class CourierShiftDay(Base):
    """Подтверждение смены за день. Одна запись на дату.

    draft → администратор ещё обрабатывает; confirmed → день закрыт
    (по каждому курьеру на смене проставлены оценка и решение по депозиту).
    """

    __tablename__ = "courier_shift_day"

    work_date: Mapped[date] = mapped_column(Date, primary_key=True)
    status: Mapped[CourierShiftDayStatus] = mapped_column(
        courier_shift_day_status_enum,
        nullable=False,
        default=CourierShiftDayStatus.DRAFT,
        server_default=CourierShiftDayStatus.DRAFT.value,
    )
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
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


class CourierShiftReview(Base):
    """Обработка курьера за день: тоглы «не оценивать» / «не собирать депозит».

    Сам факт оценки/операции по депозиту не хранится здесь — вычисляется по
    courier_evaluation (evaluated_at = day) и courier_deposit_transaction
    (transaction_date = day). Здесь только явные отказы администратора.
    """

    __tablename__ = "courier_shift_review"

    work_date: Mapped[date] = mapped_column(Date, primary_key=True)
    courier_employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employee.id", ondelete="CASCADE"),
        primary_key=True,
    )
    eval_skipped: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    deposit_skipped: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    deposit_skip_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
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

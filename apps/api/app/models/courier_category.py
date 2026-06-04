from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, func, text
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CourierCategory(str, enum.Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"


courier_category_enum = SQLEnum(
    CourierCategory,
    name="courier_category",
    values_callable=lambda values: [item.value for item in values],
)


class CourierCategoryAssignment(Base):
    __tablename__ = "courier_category_assignment"
    __table_args__ = (
        CheckConstraint(
            "effective_to is null or effective_to >= effective_from",
            name="ck_courier_category_assignment_effective_range",
        ),
        Index(
            "ix_courier_category_assignment_employee_dates",
            "employee_id",
            "effective_from",
            "effective_to",
        ),
        Index(
            "uq_courier_category_assignment_one_open",
            "employee_id",
            unique=True,
            postgresql_where=text("effective_to is null"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employee.id", ondelete="CASCADE"),
        nullable=False,
    )
    category: Mapped[CourierCategory] = mapped_column(courier_category_enum, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
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

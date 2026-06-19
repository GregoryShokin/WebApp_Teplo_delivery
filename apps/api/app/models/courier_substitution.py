from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CourierShiftSubstitution(Base):
    """Подмена плейсхолдер-курьера реальным сотрудником за конкретный день.

    Когда курьер без собственной карточки iiko открывает смену под общей
    плейсхолдер-карточкой («Курьер 1»), оператор на экране смены указывает, кто
    реально работал. Оценки, депозит и KPI за этот день относятся к
    real_employee_id, а не к плейсхолдеру.

    Одна плейсхолдер-карточка iiko = одна открытая смена за раз, поэтому ключ
    (work_date, placeholder_employee_id) уникален; несколько подменных в один день
    решаются несколькими плейсхолдер-карточками.
    """

    __tablename__ = "courier_shift_substitution"
    __table_args__ = (
        CheckConstraint(
            "placeholder_employee_id <> real_employee_id",
            name="ck_courier_shift_substitution_distinct",
        ),
        UniqueConstraint(
            "work_date",
            "placeholder_employee_id",
            name="uq_courier_shift_substitution_date_placeholder",
        ),
        Index(
            "ix_courier_shift_substitution_real_date",
            "real_employee_id",
            "work_date",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    placeholder_employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employee.id", ondelete="CASCADE"),
        nullable=False,
    )
    real_employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employee.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
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

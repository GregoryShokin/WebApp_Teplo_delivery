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
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class FreelancerTempCard(Base):
    """Временная карточка внештатника, привязанная к плейсхолдеру пула «Внештат №N».

    Реальный внештатник — отдельная строка `employee` (реальное имя, категория
    freelancer, синтетический iiko_id). Эта запись связывает его с системным
    плейсхолдером (`placeholder_employee_id`, у которого настоящая iiko-карта) на
    период жизни карточки. Явка из iiko приходит на плейсхолдер и по дате
    перенаправляется на `employee_id`.

    Перекрытие периодов на одном плейсхолдере невозможно по построению (аллокатор
    берёт свободный №N), поэтому пара (placeholder, дата) однозначно резолвится в
    одного внештатника.
    """

    __tablename__ = "freelancer_temp_card"
    __table_args__ = (
        CheckConstraint(
            "period_to >= period_from",
            name="ck_freelancer_temp_card_period_order",
        ),
        # Максимум 30 дней (включительно): period_to - period_from <= 29.
        CheckConstraint(
            "period_to - period_from <= 29",
            name="ck_freelancer_temp_card_period_max_30_days",
        ),
        CheckConstraint(
            "placeholder_employee_id <> employee_id",
            name="ck_freelancer_temp_card_distinct",
        ),
        UniqueConstraint(
            "employee_id",
            name="uq_freelancer_temp_card_employee",
        ),
        Index(
            "ix_freelancer_temp_card_placeholder_period",
            "placeholder_employee_id",
            "period_from",
            "period_to",
        ),
        Index(
            "ix_freelancer_temp_card_active",
            "archived_at",
            "period_to",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employee.id", ondelete="CASCADE"),
        nullable=False,
    )
    placeholder_employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employee.id", ondelete="RESTRICT"),
        nullable=False,
    )
    period_from: Mapped[date] = mapped_column(Date, nullable=False)
    period_to: Mapped[date] = mapped_column(Date, nullable=False)
    # ПИН открытия смены — хранится ОТКРЫТЫМ ТЕКСТОМ (4-значный ПИН временного
    # сотрудника, низкая чувствительность): управляющий должен всегда видеть ПИН,
    # чтобы передать его внештатнику. У самой строки-внештатника ПИН только хешем,
    # значение оттуда не достать — поэтому дублируем сюда.
    pin_code: Mapped[str | None] = mapped_column(String(4), nullable=True)
    # Проставляется автоархивацией (или ручной архивацией) — карточка исчезает из
    # штатных списков, плейсхолдер освобождается. Физического удаления нет.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Плейсхолдер пула «Внештат №N», к которому привязана карточка. Грузим eager,
    # чтобы имя плейсхолдера было доступно при сериализации без async lazy-load.
    placeholder = relationship(
        "Employee",
        foreign_keys=[placeholder_employee_id],
        lazy="selectin",
        viewonly=True,
    )

    @property
    def placeholder_name(self) -> str | None:
        """Отображаемое имя плейсхолдера («Внештат №N») или None."""
        placeholder = self.placeholder
        return placeholder.full_name if placeholder is not None else None


class FreelancerAttendanceCase(Base):
    """Явка на плейсхолдер пула в дату, когда к нему не привязан внештатник.

    Не теряем такую явку: заводим кейс разбора менеджеру (по образцу существующих
    reconciliation-кейсов). Идемпотентно по (placeholder, work_date): при
    перестройке леджера обновляется, при появлении привязки — резолвится.
    """

    __tablename__ = "freelancer_attendance_case"
    __table_args__ = (
        CheckConstraint(
            "status in ('open', 'resolved', 'dismissed')",
            name="ck_freelancer_attendance_case_status",
        ),
        UniqueConstraint(
            "placeholder_employee_id",
            "work_date",
            name="uq_freelancer_attendance_case_placeholder_date",
        ),
        Index(
            "ix_freelancer_attendance_case_status",
            "status",
            "work_date",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    placeholder_employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employee.id", ondelete="CASCADE"),
        nullable=False,
    )
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="open", server_default="open"
    )
    resolved_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employee.id", ondelete="SET NULL"),
        nullable=True,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

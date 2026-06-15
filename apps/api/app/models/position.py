from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

POSITION_ARCHETYPES = ("okladnik", "production_percent", "shift_pool", "courier", "none")
POSITION_PERMISSION_GROUPS = (
    "administration",
    "cooks",
    "cashiers",
    "auxiliary",
    "couriers",
    "none",
)
POSITION_STATUSES = ("active", "excluded")
POSITION_SCHEDULE_TYPES = ("SESSION", "FIXED", "HOURS")


class Position(Base):
    """Реестр должностей: архетип оплаты + группа прав + допуски + связь с ролью iiko."""

    __tablename__ = "position"
    __table_args__ = (
        CheckConstraint(
            "archetype in ('okladnik', 'production_percent', 'shift_pool', 'courier', 'none')",
            name="ck_position_archetype",
        ),
        CheckConstraint(
            "permission_group in "
            "('administration', 'cooks', 'cashiers', 'auxiliary', 'couriers', 'none')",
            name="ck_position_permission_group",
        ),
        CheckConstraint("status in ('active', 'excluded')", name="ck_position_status"),
        CheckConstraint(
            "schedule_type in ('SESSION', 'FIXED', 'HOURS')",
            name="ck_position_schedule_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    iiko_role_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    iiko_role_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    archetype: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    permission_group: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    # Допуски (отпуска / график / надбавка старшинства / подмена) больше не хранятся
    # как отдельные флаги — они выводятся из archetype (см. position_registry):
    # archetype == production_percent → все включены, иначе выключены.
    # Участие должности в выдаче доступов (RBAC). Если True — должность попадает в
    # список «Права должностей» и связана с ролью-движком прав через access_role_code.
    participates_in_access: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    access_role_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Курьер-окладник: помимо своего архетипа получает админ-оклад в полумесячной
    # ведомости (например «Старший курьер» — archetype=courier + админ-оклад).
    gets_admin_oklad: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    schedule_type: Mapped[str] = mapped_column(String(16), nullable=False, default="SESSION")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class PositionChangeEvent(Base):
    """Журнал изменений реестра должностей (по образцу журнала сотрудников)."""

    __tablename__ = "position_change_event"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("position.id", ondelete="SET NULL"), nullable=True
    )
    position_name: Mapped[str] = mapped_column(String(160), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    before_value: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    after_value: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

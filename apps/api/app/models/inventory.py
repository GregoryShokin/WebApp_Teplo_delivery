from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class InventoryAuditPosition(Base):
    __tablename__ = "inventory_audit_position"
    __table_args__ = (
        CheckConstraint(
            "allocation_group IS NULL OR allocation_group IN ('chefs','common','admins')",
            name="ck_inventory_audit_position_group",
        ),
        UniqueConstraint("code", name="uq_inventory_audit_position_code"),
        Index("ix_inventory_audit_position_active", "is_active", "sort_order"),
        Index("ix_inventory_audit_position_guid", "iiko_product_guid"),
        Index("ix_inventory_audit_position_swap_group", "swap_group"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    allocation_group: Mapped[str | None] = mapped_column(String(16), nullable=True)
    swap_group: Mapped[str | None] = mapped_column(Text, nullable=True)
    iiko_product_guid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    sort_order: Mapped[int] = mapped_column(nullable=False, default=100, server_default="100")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class InventoryAudit(Base):
    __tablename__ = "inventory_audit"
    __table_args__ = (
        CheckConstraint("source IN ('iiko','manual')", name="ck_inventory_audit_source"),
        CheckConstraint(
            "status IN ('draft','applied','cancelled')",
            name="ck_inventory_audit_status",
        ),
        UniqueConstraint("business_date", name="uq_inventory_audit_business_date"),
        Index("ix_inventory_audit_date", "business_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    previous_audit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    iiko_document_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    iiko_document_num: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="iiko",
        server_default="iiko",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="draft",
        server_default="draft",
    )
    total_shortage_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        default=0,
        server_default="0",
    )
    total_penalty_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        default=0,
        server_default="0",
    )
    computation_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Перенос даты списания штрафа: если задано — work_date берётся отсюда,
    # иначе действует правило по умолчанию (business_date + 1 день).
    penalty_work_date_override: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    applied_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    items: Mapped[list[InventoryAuditItem]] = relationship(
        back_populates="audit",
        cascade="all, delete-orphan",
        order_by=lambda: InventoryAuditItem.created_at,
    )
    employee_exclusions: Mapped[list[InventoryAuditEmployeeExclusion]] = relationship(
        back_populates="audit",
        cascade="all, delete-orphan",
        order_by=lambda: InventoryAuditEmployeeExclusion.created_at,
    )
    item_exclusions: Mapped[list[InventoryAuditItemExclusion]] = relationship(
        back_populates="audit",
        cascade="all, delete-orphan",
        order_by=lambda: InventoryAuditItemExclusion.created_at,
    )


class InventoryAuditItem(Base):
    __tablename__ = "inventory_audit_item"
    __table_args__ = (
        CheckConstraint(
            "shortage_amount >= 0",
            name="ck_inventory_audit_item_amount_non_negative",
        ),
        Index("ix_inventory_audit_item_audit", "audit_id"),
        Index("ix_inventory_audit_item_position", "position_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    audit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inventory_audit.id", ondelete="CASCADE"), nullable=False
    )
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("inventory_audit_position.id", ondelete="SET NULL"), nullable=True
    )
    iiko_product_guid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    product_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    shortage_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    swap_group_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Ручная корректировка суммы недостачи (костыль межревизионного пересорта): знаковая
    # дельта к amount, effective = amount + manual_shortage_adjustment. Исходная сумма iiko
    # (amount/shortage_amount) остаётся нетронутой. Используется, когда излишек предыдущей
    # ревизии «всплывает» ложной недостачей в текущей.
    manual_shortage_adjustment: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), nullable=True
    )
    manual_shortage_adjustment_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_shortage_adjustment_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    manual_shortage_adjustment_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    audit: Mapped[InventoryAudit] = relationship(back_populates="items")
    position: Mapped[InventoryAuditPosition | None] = relationship()


class InventoryAuditEmployeeExclusion(Base):
    __tablename__ = "inventory_audit_employee_exclusion"
    __table_args__ = (
        UniqueConstraint("audit_id", "employee_id", name="uq_inventory_audit_employee_exclusion"),
        Index("ix_inventory_audit_employee_exclusion_audit", "audit_id"),
        Index("ix_inventory_audit_employee_exclusion_employee", "employee_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    audit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inventory_audit.id", ondelete="CASCADE"), nullable=False
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    audit: Mapped[InventoryAudit] = relationship(back_populates="employee_exclusions")
    employee: Mapped[Any] = relationship("Employee")
    created_by: Mapped[Any] = relationship(
        "User",
        lazy="raise",
        foreign_keys=[created_by_user_id],
    )


class InventoryAuditItemExclusion(Base):
    __tablename__ = "inventory_audit_item_exclusion"
    __table_args__ = (
        UniqueConstraint("audit_id", "item_id", name="uq_inventory_audit_item_exclusion"),
        Index("ix_inventory_audit_item_exclusion_audit", "audit_id"),
        Index("ix_inventory_audit_item_exclusion_item", "item_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    audit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inventory_audit.id", ondelete="CASCADE"), nullable=False
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inventory_audit_item.id", ondelete="CASCADE"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    audit: Mapped[InventoryAudit] = relationship(back_populates="item_exclusions")
    item: Mapped[InventoryAuditItem] = relationship()
    created_by: Mapped[Any] = relationship(
        "User",
        lazy="raise",
        foreign_keys=[created_by_user_id],
    )

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import employee_status_enum


class Employee(Base):
    __tablename__ = "employee"
    __table_args__ = (
        CheckConstraint(
            "position in "
            "('Кассир', 'Повар', 'Управляющий', 'Системный администратор', 'Курьер', 'Менеджер')",
            name="ck_employee_position_canonical",
        ),
        CheckConstraint(
            "category is null or category in "
            "('category_1', 'category_2', 'category_3', 'category_4', 'intern', 'freelancer')",
            name="ck_employee_category_value",
        ),
        CheckConstraint(
            "default_cooking_station is null or default_cooking_station in "
            "('sushi', 'pizza', 'shawarma')",
            name="ck_employee_default_cooking_station_value",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    full_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="source=iiko; read-only in app",
        info={"source": "iiko", "read_only": True},
    )
    iiko_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    position: Mapped[str] = mapped_column(String(160), nullable=False, comment="source=iiko")
    category: Mapped[str | None] = mapped_column(Text, nullable=True, comment="source=app_managed")
    default_cooking_station: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="source=app_managed"
    )
    is_senior: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="source=app_managed",
    )
    is_deputy_senior: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="source=app_managed",
    )
    status: Mapped[str] = mapped_column(
        employee_status_enum,
        nullable=False,
        default="active",
        server_default="active",
        comment="source=app_managed",
    )
    hire_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="source=app_managed"
    )
    fire_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="source=app_managed"
    )
    fire_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="source=app_managed"
    )
    pin_hash: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="source=app_managed"
    )
    pin_set_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="source=app_managed"
    )
    iiko_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="source=iiko"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    role_assignments: Mapped[list[EmployeeRoleAssignment]] = relationship(
        back_populates="employee",
        cascade="all, delete-orphan",
        order_by=lambda: (
            EmployeeRoleAssignment.is_primary.desc(),
            EmployeeRoleAssignment.payroll_role,
        ),
    )

    @property
    def assignments(self) -> list[EmployeeRoleAssignment]:
        today = date.today()
        return [
            assignment
            for assignment in self.role_assignments
            if assignment.effective_from <= today
            and (assignment.effective_to is None or assignment.effective_to > today)
        ]


class EmployeeRoleAssignment(Base):
    __tablename__ = "employee_role_assignment"
    __table_args__ = (
        CheckConstraint(
            "payroll_role in ('sushi', 'pizza', 'shawarma', 'prep', 'administrator')",
            name="ck_employee_role_assignment_payroll_role_value",
        ),
        CheckConstraint(
            "category in "
            "('category_1', 'category_2', 'category_3', 'category_4', 'intern', 'freelancer')",
            name="ck_employee_role_assignment_category_value",
        ),
        CheckConstraint(
            "effective_to is null or effective_to >= effective_from",
            name="ck_employee_role_assignment_effective_range",
        ),
        UniqueConstraint(
            "employee_id",
            "payroll_role",
            "effective_from",
            name="uq_employee_role_assignment_employee_role_effective_from",
        ),
        Index(
            "ix_employee_role_assignment_employee_active",
            "employee_id",
            "effective_from",
            "effective_to",
        ),
        Index(
            "uq_employee_role_assignment_one_open_primary",
            "employee_id",
            unique=True,
            postgresql_where=text("is_primary = true and effective_to is null"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="CASCADE"), nullable=False
    )
    payroll_role: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    is_primary: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    employee: Mapped[Employee] = relationship(back_populates="role_assignments")

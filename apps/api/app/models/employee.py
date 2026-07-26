from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    column,
    func,
    select,
    table,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, column_property, mapped_column, relationship

from app.db.base import Base
from app.models.enums import employee_status_enum

if TYPE_CHECKING:
    from app.models.employee_position_assignment import EmployeePositionAssignment
    from app.models.freelancer import FreelancerTempCard

_employee_position_assignment = table(
    "employee_position_assignment",
    column("employee_id", UUID(as_uuid=True)),
    column("position", String),
    column("effective_from", Date),
    column("effective_to", Date),
)


class Employee(Base):
    __tablename__ = "employee"
    __table_args__ = (
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
        CheckConstraint(
            "not (pin_assumed_from_iiko = true and pin_hash is not null)",
            name="pin_origin_exclusive",
        ),
        CheckConstraint(
            "deposit_target_override is null or deposit_target_override >= 0",
            name="deposit_target_override_non_negative",
        ),
        CheckConstraint(
            "deposit_withholding_override is null or deposit_withholding_override >= 0",
            name="deposit_withholding_override_non_negative",
        ),
        CheckConstraint(
            "freelancer_shift_rate is null or freelancer_shift_rate > 0",
            name="ck_employee_freelancer_shift_rate_positive",
        ),
        CheckConstraint(
            "official_status in ('working', 'maternity_leave')",
            name="ck_employee_official_status_value",
        ),
        CheckConstraint(
            "official_salary is null or official_salary > 0",
            name="ck_employee_official_salary_positive",
        ),
        CheckConstraint(
            "official_children_count >= 0",
            name="ck_employee_official_children_non_negative",
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
    position: Mapped[str | None] = column_property(
        select(_employee_position_assignment.c.position)
        .where(
            _employee_position_assignment.c.employee_id == id,
            _employee_position_assignment.c.effective_from <= func.current_date(),
            (
                _employee_position_assignment.c.effective_to.is_(None)
                | (_employee_position_assignment.c.effective_to >= func.current_date())
            ),
        )
        .order_by(_employee_position_assignment.c.effective_from.desc())
        .limit(1)
        .scalar_subquery(),
        expire_on_flush=False,
    )
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
    is_courier_placeholder: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="source=app_managed; общая карточка для смен курьеров без своей карточки iiko",
    )
    is_freelancer_placeholder: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment=(
            "source=app_managed; системная карточка пула «Внештат №N» — держит "
            "реальную iiko-карту, к которой привязываются временные внештатники; "
            "скрыта из штатных списков"
        ),
    )
    is_freelancer_temp: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment=(
            "source=app_managed; временный внештатник («однодневка») с реальным "
            "именем, привязан к плейсхолдеру пула; своей iiko-карты нет"
        ),
    )
    freelancer_shift_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2),
        nullable=True,
        comment=(
            "source=app_managed; договорная ставка внештатника за 12-часовую смену, ₽. "
            "Начисление = ставка/12 × фактические часы явки (без доплат пт/сб и +1ч)"
        ),
    )
    # ── Официальный зарплатный контур (решение владельца 26.07.2026) ─────────────
    # Официальные данные ≠ внутренние: имя в iiko может быть псевдонимом («Victoria
    # Manager»), реальная зарплата — отличаться от официального оклада. Синк iiko эти
    # поля НЕ трогает; чтение/правка — только под правами staff.official.read/manage.
    # По официальным сотрудникам налоговый модуль прогнозирует начисления (НДФЛ,
    # взносы МСП, травматизм) за отработанные месяцы до прихода оборотки бухгалтера.
    is_official: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="source=app_managed; сотрудник оформлен официально (трудовой договор)",
    )
    official_full_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="source=app_managed; ФИО по трудовому договору — для сверки с документами ФНС",
    )
    official_tab_number: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="source=app_managed; табельный номер — ключ сверки с обороткой бухгалтера",
    )
    official_salary: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2),
        nullable=True,
        comment="source=app_managed; официальный оклад по трудовому договору, ₽/мес",
    )
    official_children_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment=(
            "source=app_managed; несовершеннолетних детей — вычет НДФЛ считается сам "
            "(ст. 218 НК: 1-й 1400, 2-й 2800, 3-й+ 6000, суммируются)"
        ),
    )
    official_single_parent: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="source=app_managed; единственный родитель — детский вычет в двойном размере",
    )
    official_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="working",
        server_default="working",
        comment="source=app_managed; working — начисления идут, maternity_leave — нет",
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
    tenure_started_at: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="source=app_managed"
    )
    fire_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="source=app_managed"
    )
    fire_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="source=app_managed"
    )
    deposit_target_override: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), nullable=True, comment="source=app_managed"
    )
    deposit_withholding_override: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), nullable=True, comment="source=app_managed"
    )
    deposit_excluded: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="source=app_managed",
    )
    deposit_excluded_until: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="source=app_managed"
    )
    deposit_excluded_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="source=app_managed"
    )
    requires_role_review: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="source=app_managed",
    )
    role_review_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="source=app_managed"
    )
    requires_position_review: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="source=app_managed",
    )
    fund_excluded: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="source=app_managed",
    )
    fund_excluded_until: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="source=app_managed"
    )
    fund_excluded_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="source=app_managed"
    )
    admin_payroll_excluded: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="source=app_managed",
    )
    pin_hash: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="source=app_managed"
    )
    pin_assumed_from_iiko: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="source=app_managed",
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
    position_events: Mapped[list[EmployeePositionEvent]] = relationship(
        back_populates="employee",
        cascade="all, delete-orphan",
        order_by=lambda: EmployeePositionEvent.effective_from.desc(),
    )
    position_assignments: Mapped[list[EmployeePositionAssignment]] = relationship(
        back_populates="employee",
        cascade="all, delete-orphan",
        order_by="EmployeePositionAssignment.effective_from.desc()",
    )
    allowance_events: Mapped[list[EmployeeAllowanceEvent]] = relationship(
        back_populates="employee",
        cascade="all, delete-orphan",
        order_by=lambda: (
            EmployeeAllowanceEvent.allowance_type,
            EmployeeAllowanceEvent.effective_from.desc(),
        ),
    )
    pending_iiko_actions: Mapped[list[EmployeePendingIikoAction]] = relationship(
        back_populates="employee",
        cascade="all, delete-orphan",
        order_by=lambda: EmployeePendingIikoAction.effective_on,
    )
    # Карточка временного внештатника (для этой строки как внештатника). Плейсхолдер
    # с обратной стороны привязки сюда НЕ попадает (только employee_id).
    freelancer_card: Mapped[FreelancerTempCard | None] = relationship(
        "FreelancerTempCard",
        primaryjoin="Employee.id == FreelancerTempCard.employee_id",
        foreign_keys="FreelancerTempCard.employee_id",
        viewonly=True,
        uselist=False,
        lazy="selectin",
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
    is_substitute: Mapped[bool] = mapped_column(
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

    @property
    def is_pending(self) -> bool:
        return self.effective_from > date.today()


class EmployeePositionEvent(Base):
    __tablename__ = "employee_position_event"
    __table_args__ = (
        CheckConstraint(
            "position in "
            "('Кассир', 'Повар', 'Управляющий', 'Системный администратор', "
            "'Курьер', 'Менеджер', 'Уборщица', 'Посудомойка')",
            name="ck_employee_position_event_position_canonical",
        ),
        CheckConstraint(
            "effective_to is null or effective_to > effective_from",
            name="ck_employee_position_event_effective_range",
        ),
        UniqueConstraint(
            "employee_id",
            "effective_from",
            name="uq_employee_position_event_employee_effective_from",
        ),
        Index(
            "ix_employee_position_event_employee_active",
            "employee_id",
            "effective_from",
            "effective_to",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[str] = mapped_column(String(160), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    employee: Mapped[Employee] = relationship(back_populates="position_events")


class EmployeeAllowanceEvent(Base):
    __tablename__ = "employee_allowance_event"
    __table_args__ = (
        CheckConstraint(
            "allowance_type in ('senior', 'deputy_senior')",
            name="ck_employee_allowance_event_type_value",
        ),
        CheckConstraint(
            "effective_to is null or effective_to > effective_from",
            name="ck_employee_allowance_event_effective_range",
        ),
        UniqueConstraint(
            "employee_id",
            "allowance_type",
            "effective_from",
            name="uq_employee_allowance_event_employee_type_effective_from",
        ),
        Index(
            "ix_employee_allowance_event_employee_active",
            "employee_id",
            "allowance_type",
            "effective_from",
            "effective_to",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="CASCADE"), nullable=False
    )
    allowance_type: Mapped[str] = mapped_column(String(32), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    employee: Mapped[Employee] = relationship(back_populates="allowance_events")


class EmployeePendingIikoAction(Base):
    __tablename__ = "employee_pending_iiko_action"
    __table_args__ = (
        CheckConstraint(
            "action_type in ('update_position')",
            name="ck_employee_pending_iiko_action_type_value",
        ),
        CheckConstraint(
            "status in ('pending', 'applied', 'failed', 'cancelled')",
            name="ck_employee_pending_iiko_action_status_value",
        ),
        Index(
            "ix_employee_pending_iiko_action_due",
            "status",
            "effective_on",
        ),
        Index(
            "ix_employee_pending_iiko_action_employee",
            "employee_id",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("employee.id", ondelete="CASCADE"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    effective_on: Mapped[date] = mapped_column(Date, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_entity_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    related_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    employee: Mapped[Employee] = relationship(back_populates="pending_iiko_actions")


class EmployeeDismissalReason(Base):
    __tablename__ = "employee_dismissal_reason"
    __table_args__ = (
        UniqueConstraint("code", name="uq_employee_dismissal_reason_code"),
        Index("ix_employee_dismissal_reason_active", "is_active", "sort_order", "label"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    requires_comment: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    is_system: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    sort_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class EmployeeChangeEvent(Base):
    __tablename__ = "employee_change_event"
    __table_args__ = (
        CheckConstraint(
            "source in ('app', 'iiko_sync', 'system_migration')",
            name="ck_employee_change_event_source_value",
        ),
        CheckConstraint(
            "status in ('success', 'error', 'requires_review', 'skipped')",
            name="ck_employee_change_event_status_value",
        ),
        Index("ix_employee_change_event_employee_changed", "employee_id", "changed_at"),
        Index("ix_employee_change_event_changed_at", "changed_at"),
        Index("ix_employee_change_event_effective_from", "effective_from"),
        Index("ix_employee_change_event_type", "change_type"),
        Index("ix_employee_change_event_source_status", "source", "status"),
        Index("ix_employee_change_event_actor_user", "actor_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employee.id", ondelete="SET NULL"),
        nullable=True,
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    change_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    actor_label: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="success")
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    before_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    diff: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    reason_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employee_dismissal_reason.id", ondelete="SET NULL"),
        nullable=True,
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason_label: Mapped[str | None] = mapped_column(String(160), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_run.id", ondelete="SET NULL"), nullable=True
    )
    related_agent_action_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_action.id", ondelete="SET NULL"), nullable=True
    )
    related_entity_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    related_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    payroll_impact: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    payroll_impact_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

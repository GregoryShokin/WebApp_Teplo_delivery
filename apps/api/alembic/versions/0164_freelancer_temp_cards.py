"""Временные внештатники через пул iiko-плейсхолдеров «Внештат №N».

- ``employee.is_freelancer_placeholder`` — системная карточка пула (держит реальную
  iiko-карту «Внештат №N»; скрыта из штатных списков; цель ремапа явок).
- ``employee.is_freelancer_temp`` — временный внештатник (реальное имя, категория
  freelancer, синтетический iiko_id); guard для reset/ghost iiko-синка.
- ``employee.freelancer_shift_rate`` — договорная ставка за 12-часовую смену, ₽
  (начисление = ставка/12 × часы явки, без доплат пт/сб и +1ч).
- ``freelancer_temp_card`` — привязка внештатника к плейсхолдеру на период (≤30 дней).
- ``freelancer_attendance_case`` — кейс разбора: явка на плейсхолдер в дату без привязки.

Со старыми карточками «Внештат №1/№2» миграция ничего не делает — их поглощение
выполняется отдельным management-скриптом ПОСЛЕ финализации ведомости
(app/scripts/absorb_legacy_freelancers.py).

Revision ID: 0162_freelancer_temp_cards
Revises: 0161_expense_safe_draft
Create Date: 2026-07-07
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.auth.permissions import PERMISSION_CATALOG

revision = "0164_freelancer_temp_cards"
down_revision = "0163_kassa_payin_presets"
branch_labels = None
depends_on = None

# Право «Смотреть ПИН внештатника»: ПИН внештатника показываем только тем, кто
# реально выдаёт смены (владелец/админ/управляющий), а не всем со Штатом.
FREELANCER_PIN_CODES = ("staff.freelancer_pin.read",)
FREELANCER_PIN_GRANT_ROLES = ("owner", "admin", "manager")


def upgrade() -> None:
    op.add_column(
        "employee",
        sa.Column(
            "is_freelancer_placeholder",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "employee",
        sa.Column(
            "is_freelancer_temp",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "employee",
        sa.Column("freelancer_shift_rate", sa.Numeric(12, 2), nullable=True),
    )
    op.create_check_constraint(
        "ck_employee_freelancer_shift_rate_positive",
        "employee",
        "freelancer_shift_rate is null or freelancer_shift_rate > 0",
    )

    op.create_table(
        "freelancer_temp_card",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("placeholder_employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("period_from", sa.Date(), nullable=False),
        sa.Column("period_to", sa.Date(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "period_to >= period_from",
            name="ck_freelancer_temp_card_period_order",
        ),
        sa.CheckConstraint(
            "period_to - period_from <= 29",
            name="ck_freelancer_temp_card_period_max_30_days",
        ),
        sa.CheckConstraint(
            "placeholder_employee_id <> employee_id",
            name="ck_freelancer_temp_card_distinct",
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["placeholder_employee_id"], ["employee.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("employee_id", name="uq_freelancer_temp_card_employee"),
    )
    # ПИН открытия смены внештатника — открытым текстом (управляющий всегда видит ПИН).
    op.add_column(
        "freelancer_temp_card",
        sa.Column("pin_code", sa.String(length=4), nullable=True),
    )
    op.create_index(
        "ix_freelancer_temp_card_placeholder_period",
        "freelancer_temp_card",
        ["placeholder_employee_id", "period_from", "period_to"],
    )
    op.create_index(
        "ix_freelancer_temp_card_active",
        "freelancer_temp_card",
        ["archived_at", "period_to"],
    )

    op.create_table(
        "freelancer_attendance_case",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("placeholder_employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("resolved_employee_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status in ('open', 'resolved', 'dismissed')",
            name="ck_freelancer_attendance_case_status",
        ),
        sa.ForeignKeyConstraint(["placeholder_employee_id"], ["employee.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resolved_employee_id"], ["employee.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "placeholder_employee_id",
            "work_date",
            name="uq_freelancer_attendance_case_placeholder_date",
        ),
    )
    op.create_index(
        "ix_freelancer_attendance_case_status",
        "freelancer_attendance_case",
        ["status", "work_date"],
    )

    # Право «Смотреть ПИН внештатника» + гранты владельцу/админу/управляющему.
    _upsert_permissions()
    _grant_permissions(FREELANCER_PIN_GRANT_ROLES, FREELANCER_PIN_CODES)


def downgrade() -> None:
    _revoke_permissions(FREELANCER_PIN_CODES)
    op.drop_index(
        "ix_freelancer_attendance_case_status",
        table_name="freelancer_attendance_case",
    )
    op.drop_table("freelancer_attendance_case")
    op.drop_index(
        "ix_freelancer_temp_card_active",
        table_name="freelancer_temp_card",
    )
    op.drop_index(
        "ix_freelancer_temp_card_placeholder_period",
        table_name="freelancer_temp_card",
    )
    op.drop_column("freelancer_temp_card", "pin_code")
    op.drop_table("freelancer_temp_card")
    op.drop_constraint(
        "ck_employee_freelancer_shift_rate_positive",
        "employee",
        type_="check",
    )
    op.drop_column("employee", "freelancer_shift_rate")
    op.drop_column("employee", "is_freelancer_temp")
    op.drop_column("employee", "is_freelancer_placeholder")


def _upsert_permissions() -> None:
    """Идемпотентно заливает весь каталог прав (по образцу 0136)."""
    permission_table = sa.table(
        "permission",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("code", sa.String()),
        sa.column("module", sa.String()),
        sa.column("description", sa.String()),
    )
    rows = [
        {"id": uuid.uuid4(), "code": code, "module": module, "description": description}
        for code, module, description in PERMISSION_CATALOG
    ]
    insert_stmt = postgresql.insert(permission_table).values(rows)
    op.get_bind().execute(
        insert_stmt.on_conflict_do_update(
            index_elements=["code"],
            set_={
                "module": insert_stmt.excluded.module,
                "description": insert_stmt.excluded.description,
            },
        )
    )


def _grant_permissions(role_codes: tuple[str, ...], permission_codes: tuple[str, ...]) -> None:
    if not role_codes or not permission_codes:
        return
    op.execute(
        f"""
        insert into role_permission (role_id, permission_id)
        select role.id, permission.id
        from role
        cross join permission
        where role.code in ({_sql_values(role_codes)})
          and permission.code in ({_sql_values(permission_codes)})
        on conflict (role_id, permission_id) do nothing
        """
    )


def _revoke_permissions(permission_codes: tuple[str, ...]) -> None:
    codes = _sql_values(permission_codes)
    op.execute(
        f"""
        delete from role_permission_event
        using permission
        where role_permission_event.permission_id = permission.id
          and permission.code in ({codes})
        """
    )
    op.execute(
        f"""
        delete from role_permission
        using permission
        where role_permission.permission_id = permission.id
          and permission.code in ({codes})
        """
    )
    op.execute(f"delete from permission where code in ({codes})")


def _sql_values(values: tuple[str, ...]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)

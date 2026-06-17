"""Касса: недостача смены → авто-штраф кассирам (Фаза 10).

1. Колонки на ``iiko_cash_shift``: ``total_sales`` (вся выручка смены из OLAP — база
   порога), ``penalty_status`` / ``penalty_review_reason`` (итог авто-штрафа).
2. Таблица ``kassa_shift_penalty`` — трассировка/идемпотентность/отмена авто-штрафов
   (один штраф = одному кассиру смены, связан с ``payroll_adjustment``).
3. Настройка ``kassa.shortage_penalty_threshold_pct`` (дефолт 1.0 % выручки) — порог,
   ниже которого недостача штраф не порождает. Хранится как number с unit '%'
   (не percent-виджет: percent ожидает долю 0..1, а тут «1.0 = 1 %»).
4. Право ``kassa.penalty.waive`` — точечная отмена авто-штрафа; грант только
   owner/admin (по образцу ``0119_kassa_permissions``).

Revision ID: 0121_kassa_shortage_penalty
Revises: 0120_kassa_cash_sales
Create Date: 2026-06-17
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.auth.permissions import PERMISSION_CATALOG

revision = "0121_kassa_shortage_penalty"
down_revision = "0120_kassa_cash_sales"
branch_labels = None
depends_on = None

THRESHOLD_SETTING_ID = "d7c1a2e3-4b5f-4c6a-8d9e-0f1a2b3c4d5e"
THRESHOLD_SETTING_KEY = "kassa.shortage_penalty_threshold_pct"

NEW_PERMISSION_CODE = "kassa.penalty.waive"
WAIVE_GRANT_ROLES = ("owner", "admin")


def upgrade() -> None:
    op.add_column("iiko_cash_shift", sa.Column("total_sales", sa.Numeric(18, 2), nullable=True))
    op.add_column(
        "iiko_cash_shift", sa.Column("penalty_status", sa.String(length=32), nullable=True)
    )
    op.add_column("iiko_cash_shift", sa.Column("penalty_review_reason", sa.Text(), nullable=True))

    op.create_table(
        "kassa_shift_penalty",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("shift_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payroll_adjustment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("waived_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("waived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("amount > 0", name="ck_kassa_shift_penalty_amount_positive"),
        sa.CheckConstraint("status in ('active', 'waived')", name="ck_kassa_shift_penalty_status"),
        sa.ForeignKeyConstraint(["shift_id"], ["iiko_cash_shift.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["payroll_adjustment_id"], ["payroll_adjustment.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["waived_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "shift_id", "employee_id", name="uq_kassa_shift_penalty_shift_employee"
        ),
    )
    op.create_index(
        "ix_kassa_shift_penalty_shift", "kassa_shift_penalty", ["shift_id"], unique=False
    )

    op.execute(
        f"""
        INSERT INTO app_setting (
            id, key, value, value_type, category, display_name,
            description, widget_type, widget_options, unit, updated_at
        )
        SELECT '{THRESHOLD_SETTING_ID}', '{THRESHOLD_SETTING_KEY}', '1.0'::jsonb, 'number',
               'kassa', 'Порог недостачи для авто-штрафа',
               'Недостача смены штрафует кассиров, только если превышает этот процент от '
               'всей выручки смены (наличные + безнал). Меньше порога — штрафа нет.',
               'number', null, '%', now()
        WHERE NOT EXISTS (SELECT 1 FROM app_setting WHERE key = '{THRESHOLD_SETTING_KEY}')
        """
    )

    _upsert_permissions()
    _grant_waive_permission()


def downgrade() -> None:
    op.execute(
        f"""
        delete from role_permission_event
        using permission
        where role_permission_event.permission_id = permission.id
          and permission.code = '{NEW_PERMISSION_CODE}'
        """
    )
    op.execute(
        f"""
        delete from role_permission
        using permission
        where role_permission.permission_id = permission.id
          and permission.code = '{NEW_PERMISSION_CODE}'
        """
    )
    op.execute(f"delete from permission where code = '{NEW_PERMISSION_CODE}'")

    op.execute(f"DELETE FROM app_setting WHERE key = '{THRESHOLD_SETTING_KEY}'")

    op.drop_index("ix_kassa_shift_penalty_shift", table_name="kassa_shift_penalty")
    op.drop_table("kassa_shift_penalty")
    op.drop_column("iiko_cash_shift", "penalty_review_reason")
    op.drop_column("iiko_cash_shift", "penalty_status")
    op.drop_column("iiko_cash_shift", "total_sales")


def _upsert_permissions() -> None:
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


def _grant_waive_permission() -> None:
    roles = ", ".join("'" + role.replace("'", "''") + "'" for role in WAIVE_GRANT_ROLES)
    op.execute(
        f"""
        insert into role_permission (role_id, permission_id)
        select role.id, permission.id
        from role
        cross join permission
        where role.code in ({roles})
          and permission.code = '{NEW_PERMISSION_CODE}'
        on conflict (role_id, permission_id) do nothing
        """
    )

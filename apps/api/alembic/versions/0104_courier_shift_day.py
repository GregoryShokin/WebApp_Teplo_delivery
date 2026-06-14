"""Подтверждение смены за день + тоглы обработки курьера.

Объединённая страница «Смена»: основной инбокс подтверждает день одной
кнопкой, когда по каждому курьеру на смене проставлены оценка и решение по
депозиту. Две новые таблицы:

- courier_shift_day — статус дня (draft/confirmed) + кто/когда подтвердил;
- courier_shift_review — per-курьер тоглы «не оценивать» / «не собирать
  депозит» (с обязательным комментарием на app-уровне).

Revision ID: 0104_courier_shift_day
Revises: 0103_auxiliary_create_permission
Create Date: 2026-06-14
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0104_courier_shift_day"
down_revision = "0103_auxiliary_create_permission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    status_enum = postgresql.ENUM(
        "draft", "confirmed", name="courier_shift_day_status"
    )
    status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "courier_shift_day",
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "draft", "confirmed", name="courier_shift_day_status", create_type=False
            ),
            server_default="draft",
            nullable=False,
        ),
        sa.Column("confirmed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["confirmed_by"],
            ["user.id"],
            name="fk_courier_shift_day_confirmed_by_user",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("work_date", name="pk_courier_shift_day"),
    )

    op.create_table(
        "courier_shift_review",
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("courier_employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "eval_skipped",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "deposit_skipped",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("deposit_skip_comment", sa.Text(), nullable=True),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["courier_employee_id"],
            ["employee.id"],
            name="fk_courier_shift_review_courier_employee_id_employee",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["user.id"],
            name="fk_courier_shift_review_updated_by_user",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint(
            "work_date", "courier_employee_id", name="pk_courier_shift_review"
        ),
    )


def downgrade() -> None:
    op.drop_table("courier_shift_review")
    op.drop_table("courier_shift_day")
    postgresql.ENUM(name="courier_shift_day_status").drop(op.get_bind(), checkfirst=True)

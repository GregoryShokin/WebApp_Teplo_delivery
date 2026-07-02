"""Ручное переопределение суммы удержания аванса/займа за период.

Окно «Удержания сотрудника»: досрочное полное/частичное гашение займа и отсрочка
авансов задают сумму удержания на период. Таблица salary_advance_recovery_override
(advance_id, period_id, amount) переопределяет дефолтную долю при расчёте ведомости
и переживает пересчёт. Аддитивно — старый код таблицу игнорирует.

Revision ID: 0152_recovery_override
Revises: 0151_advance_backdate
Create Date: 2026-07-02
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0152_recovery_override"
down_revision = "0151_advance_backdate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "salary_advance_recovery_override",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("advance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("period_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
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
        sa.ForeignKeyConstraint(["advance_id"], ["salary_advance.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["period_id"], ["payroll_period.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("advance_id", "period_id", name="uq_advance_recovery_override"),
        sa.CheckConstraint(
            "amount >= 0", name="ck_advance_recovery_override_amount_non_negative"
        ),
    )
    op.create_index(
        "ix_advance_recovery_override_period",
        "salary_advance_recovery_override",
        ["period_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_advance_recovery_override_period",
        table_name="salary_advance_recovery_override",
    )
    op.drop_table("salary_advance_recovery_override")

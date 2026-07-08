"""Депозит-выдача через ведомость: привязка запланированной выдачи к выбранному периоду.

При увольнении «через ведомость» намерение выдать депозит привязывается к конкретной
нефинализированной ведомости (в т.ч. будущей). Плавающие планы (target_period_id IS NULL)
сохраняют прежнее поведение — берутся в ближайший расчёт.

Revision ID: 0166_dismiss_deposit_period
Revises: 0165_freelancer_shift_settle
Create Date: 2026-07-08
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0166_dismiss_deposit_period"
down_revision = "0165_freelancer_shift_settle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deposit_payout_schedule",
        sa.Column("target_period_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_deposit_payout_schedule_target_period",
        "deposit_payout_schedule",
        "payroll_period",
        ["target_period_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_deposit_payout_schedule_target_period",
        "deposit_payout_schedule",
        type_="foreignkey",
    )
    op.drop_column("deposit_payout_schedule", "target_period_id")

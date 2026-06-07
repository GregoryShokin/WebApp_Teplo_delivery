"""add payroll run lifecycle event log

Revision ID: 0072_payroll_run_event
Revises: 0071_drop_production_umbrella
Create Date: 2026-06-07
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0072_payroll_run_event"
down_revision = "0071_drop_production_umbrella"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payroll_run_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("period_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["user.id"],
            name="fk_payroll_run_event_actor_user_id_user",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["period_id"],
            ["payroll_period.id"],
            name="fk_payroll_run_event_period_id_payroll_period",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["payroll_run.id"],
            name="fk_payroll_run_event_run_id_payroll_run",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_payroll_run_event_run_created_at",
        "payroll_run_event",
        ["run_id", "created_at"],
    )
    op.create_index(
        "ix_payroll_run_event_created_at",
        "payroll_run_event",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_payroll_run_event_created_at", table_name="payroll_run_event")
    op.drop_index("ix_payroll_run_event_run_created_at", table_name="payroll_run_event")
    op.drop_table("payroll_run_event")

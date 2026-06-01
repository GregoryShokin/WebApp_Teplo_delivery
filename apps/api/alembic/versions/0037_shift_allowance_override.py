"""shift allowance override

Revision ID: 0037_shift_allowance_override
Revises: 0036_seniority_allowance_fixed
Create Date: 2026-06-01
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0037_shift_allowance_override"
down_revision = "0036_seniority_allowance_fixed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shift_allowance_override",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("shift_schedule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("position", sa.String(160), nullable=False),
        sa.Column("recipient_employee_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("recipient_role", sa.String(16), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("set_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "set_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["shift_schedule_id"], ["shift_schedule.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recipient_employee_id"], ["employee.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["set_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "recipient_role IN ('senior', 'deputy_senior', 'none')",
            name="ck_shift_allowance_override_recipient_role",
        ),
        sa.CheckConstraint(
            "position = 'Кассир'",
            name="ck_shift_allowance_override_position_cashier_only",
        ),
        sa.CheckConstraint(
            "(recipient_role = 'none' AND recipient_employee_id IS NULL) OR "
            "(recipient_role IN ('senior','deputy_senior') "
            "AND recipient_employee_id IS NOT NULL)",
            name="ck_shift_allowance_override_recipient_consistency",
        ),
        sa.UniqueConstraint(
            "shift_schedule_id",
            "business_date",
            "position",
            name="uq_shift_allowance_override_schedule_date_position",
        ),
    )
    op.create_index(
        "ix_shift_allowance_override_date_position",
        "shift_allowance_override",
        ["business_date", "position"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_shift_allowance_override_date_position",
        table_name="shift_allowance_override",
    )
    op.drop_table("shift_allowance_override")

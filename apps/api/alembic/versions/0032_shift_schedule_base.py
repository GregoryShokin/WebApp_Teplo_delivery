"""add shift schedule base

Revision ID: 0032_shift_schedule_base
Revises: 0031_fund_exclusion
Create Date: 2026-06-01
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0032_shift_schedule_base"
down_revision = "0031_fund_exclusion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shift_schedule",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("date_start", sa.Date(), nullable=False),
        sa.Column("date_end", sa.Date(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
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
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("superseded_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["published_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["superseded_by_id"], ["shift_schedule.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'superseded')",
            name="ck_shift_schedule_status",
        ),
        sa.CheckConstraint("date_end >= date_start", name="ck_shift_schedule_date_range"),
    )
    op.create_index(
        "ix_shift_schedule_status_date_start",
        "shift_schedule",
        ["status", "date_start"],
    )

    op.create_table(
        "scheduled_shift",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("shift_schedule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payroll_role", sa.String(64), nullable=False),
        sa.Column("station_code", sa.String(64), nullable=True),
        sa.Column("planned_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("comment_private", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
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
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "planned_end_at > planned_start_at",
            name="ck_scheduled_shift_interval",
        ),
        sa.UniqueConstraint(
            "shift_schedule_id",
            "business_date",
            "employee_id",
            name="uq_scheduled_shift_schedule_date_employee",
        ),
    )
    op.create_index(
        "ix_scheduled_shift_schedule_date",
        "scheduled_shift",
        ["shift_schedule_id", "business_date"],
    )
    op.create_index(
        "ix_scheduled_shift_employee_date",
        "scheduled_shift",
        ["employee_id", "business_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_shift_employee_date", table_name="scheduled_shift")
    op.drop_index("ix_scheduled_shift_schedule_date", table_name="scheduled_shift")
    op.drop_table("scheduled_shift")
    op.drop_index("ix_shift_schedule_status_date_start", table_name="shift_schedule")
    op.drop_table("shift_schedule")

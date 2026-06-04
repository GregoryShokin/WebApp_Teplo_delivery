"""add courier iiko shifts and matching

Revision ID: 0057_courier_iiko_shift_matching
Revises: 0056_courier_backend
Create Date: 2026-06-04
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0057_courier_iiko_shift_matching"
down_revision = "0056_courier_backend"
branch_labels = None
depends_on = None


def upgrade() -> None:
    match_status_enum = postgresql.ENUM(
        "matched",
        "no_show",
        "helping",
        "short_shift",
        name="courier_shift_match_status",
        create_type=False,
    )
    match_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "courier_iiko_shift",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("iiko_employee_id", sa.String(length=128), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("iiko_role_id", sa.String(length=128), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attendance_type", sa.String(length=8), nullable=False),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "raw_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
            name="fk_courier_iiko_shift_employee_id_employee",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "iiko_employee_id",
            "opened_at",
            name="uq_courier_iiko_shift_iiko_employee_opened",
        ),
    )
    op.create_index(
        "ix_courier_iiko_shift_employee_opened",
        "courier_iiko_shift",
        ["employee_id", "opened_at"],
    )
    op.create_index(
        "ix_courier_iiko_shift_opened_at",
        "courier_iiko_shift",
        ["opened_at"],
    )

    op.create_table(
        "courier_shift_match",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("courier_employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("schedule_entry_id", sa.Integer(), nullable=True),
        sa.Column("iiko_shift_id", sa.Integer(), nullable=True),
        sa.Column("status", match_status_enum, nullable=False),
        sa.Column("late_minutes", sa.Integer(), nullable=True),
        sa.Column("worked_minutes", sa.Integer(), nullable=True),
        sa.Column("deliveries_count", sa.Integer(), nullable=True),
        sa.Column(
            "recalculated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["courier_employee_id"],
            ["employee.id"],
            name="fk_courier_shift_match_courier_employee_id_employee",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["schedule_entry_id"],
            ["courier_schedule_entry.id"],
            name="fk_courier_shift_match_schedule_entry_id_courier_schedule_entry",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["iiko_shift_id"],
            ["courier_iiko_shift.id"],
            name="fk_courier_shift_match_iiko_shift_id_courier_iiko_shift",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "courier_employee_id",
            "work_date",
            "schedule_entry_id",
            "iiko_shift_id",
            name="uq_courier_shift_match_courier_day_plan_fact",
        ),
    )
    op.create_index(
        "ix_courier_shift_match_courier_date",
        "courier_shift_match",
        ["courier_employee_id", "work_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_courier_shift_match_courier_date",
        table_name="courier_shift_match",
    )
    op.drop_table("courier_shift_match")
    op.drop_index(
        "ix_courier_iiko_shift_opened_at",
        table_name="courier_iiko_shift",
    )
    op.drop_index(
        "ix_courier_iiko_shift_employee_opened",
        table_name="courier_iiko_shift",
    )
    op.drop_table("courier_iiko_shift")
    postgresql.ENUM(name="courier_shift_match_status").drop(op.get_bind(), checkfirst=True)

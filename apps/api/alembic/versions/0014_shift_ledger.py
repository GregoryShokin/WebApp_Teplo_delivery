"""add daily shift ledger

Revision ID: 0014_shift_ledger
Revises: 0013_employee_role_assignments
Create Date: 2026-05-28
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0014_shift_ledger"
down_revision = "0013_employee_role_assignments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shift_ledger_entry",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payroll_role", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_resolved", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.CheckConstraint(
            "source in ('schedule', 'manual_correction', 'fallback_primary')",
            name="ck_shift_ledger_entry_source",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
            name="fk_shift_ledger_entry_employee_id_employee",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "work_date",
            "employee_id",
            name="uq_shift_ledger_entry_work_date_employee",
        ),
    )
    op.create_index(
        "ix_shift_ledger_entry_work_date",
        "shift_ledger_entry",
        ["work_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_shift_ledger_entry_work_date", table_name="shift_ledger_entry")
    op.drop_table("shift_ledger_entry")

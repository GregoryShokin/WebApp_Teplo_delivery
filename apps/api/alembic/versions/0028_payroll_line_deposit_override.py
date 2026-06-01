"""add payroll line deposit override

Revision ID: 0028_line_deposit_override
Revises: 0027_payroll_adjustments
Create Date: 2026-05-31
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0028_line_deposit_override"
down_revision = "0027_payroll_adjustments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payroll_line",
        sa.Column(
            "deposit_excluded_for_run",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "payroll_line",
        sa.Column("deposit_exclusion_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("payroll_line", "deposit_exclusion_reason")
    op.drop_column("payroll_line", "deposit_excluded_for_run")

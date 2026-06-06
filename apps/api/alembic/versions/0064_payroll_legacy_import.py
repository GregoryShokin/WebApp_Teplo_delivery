"""add payroll legacy import fields

Revision ID: 0064_payroll_legacy_import
Revises: 0063_employee_position_assign
Create Date: 2026-06-06
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0064_payroll_legacy_import"
down_revision = "0063_employee_position_assign"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payroll_line",
        sa.Column(
            "ndfl_withheld",
            sa.Numeric(14, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "payroll_run",
        sa.Column(
            "is_imported_legacy",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("payroll_run", "is_imported_legacy")
    op.drop_column("payroll_line", "ndfl_withheld")

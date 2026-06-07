"""require active payroll rates to have amount

Revision ID: 0075_payroll_rate_active_amount
Revises: 0074_payroll_payouts
Create Date: 2026-06-07
"""

from __future__ import annotations

from alembic import op

revision = "0075_payroll_rate_active_amount"
down_revision = "0074_payroll_payouts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        op.f("ck_payroll_rate_active_amount"),
        "payroll_rate",
        "(is_active = false) OR (amount IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_payroll_rate_active_amount"), "payroll_rate", type_="check")

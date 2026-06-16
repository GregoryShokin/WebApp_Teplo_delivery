"""vacation payout date (single-tranche vacation pay)

Adds ``vacation_period.payout_date`` (DATE NULL). When set, the full vacation
pay (``days_count × vacation.daily_amount``) is paid as a single tranche in the
weekly payroll run whose ``payroll_date`` equals this date (a Tuesday), instead
of being spread per-day across the weeks the vacation overlaps. Legacy rows with
NULL ``payout_date`` keep the per-day behaviour.

Revision ID: 0116_vacation_payout_date
Revises: 0115_real_bank_wallets
Create Date: 2026-06-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0116_vacation_payout_date"
down_revision = "0115_real_bank_wallets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "vacation_period",
        sa.Column("payout_date", sa.Date(), nullable=True),
    )
    op.create_index(
        "ix_vacation_period_payout_date",
        "vacation_period",
        ["payout_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_vacation_period_payout_date", table_name="vacation_period")
    op.drop_column("vacation_period", "payout_date")

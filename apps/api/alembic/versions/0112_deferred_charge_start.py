"""deferred audit charge start period

Adds ``deferred_audit_charge.start_period_start`` (DATE NULL). When set, the
deferred charge's splits are scheduled to consecutive weekly payroll periods
starting from this period (split N → start + 7*(N-1) days), instead of landing
on the next available runs. NULL keeps the legacy "next runs" behaviour.

Revision ID: 0112_deferred_charge_start
Revises: 0111_audit_penalty_date_override
Create Date: 2026-06-16
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0112_deferred_charge_start"
down_revision = "0111_audit_penalty_date_override"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deferred_audit_charge",
        sa.Column("start_period_start", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("deferred_audit_charge", "start_period_start")

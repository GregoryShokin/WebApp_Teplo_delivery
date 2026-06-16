"""inventory audit penalty work_date override

Adds ``inventory_audit.penalty_work_date_override`` (DATE NULL). When set, it
overrides the default penalty ``work_date`` (``business_date + 1 day``) so the
revision shortage can be deducted in a chosen, still-open payroll period.

Revision ID: 0111_audit_penalty_date_override
Revises: 0110_warehouse_phase4_seed
Create Date: 2026-06-16
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0111_audit_penalty_date_override"
down_revision = "0110_warehouse_phase4_seed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inventory_audit",
        sa.Column("penalty_work_date_override", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("inventory_audit", "penalty_work_date_override")

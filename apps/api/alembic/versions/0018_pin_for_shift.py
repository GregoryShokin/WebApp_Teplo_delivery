"""add employee shift PIN metadata

Revision ID: 0018_pin_for_shift
Revises: 0017_ledger_multi_shifts
Create Date: 2026-05-29
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0018_pin_for_shift"
down_revision = "0017_ledger_multi_shifts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employee",
        sa.Column("pin_hash", sa.String(length=255), nullable=True, comment="source=app_managed"),
    )
    op.add_column(
        "employee",
        sa.Column(
            "pin_set_at", sa.DateTime(timezone=True), nullable=True, comment="source=app_managed"
        ),
    )


def downgrade() -> None:
    op.drop_column("employee", "pin_set_at")
    op.drop_column("employee", "pin_hash")

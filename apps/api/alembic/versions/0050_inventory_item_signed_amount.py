"""ensure inventory audit item signed amount

Revision ID: 0050_inv_item_signed_amount
Revises: 0049_inventory_swap_groups
Create Date: 2026-06-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0050_inv_item_signed_amount"
down_revision = "0049_inventory_swap_groups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE inventory_audit_item
        ADD COLUMN IF NOT EXISTS amount numeric(14, 2)
        """
    )
    op.execute("UPDATE inventory_audit_item SET amount = -shortage_amount WHERE amount IS NULL")
    op.alter_column(
        "inventory_audit_item",
        "amount",
        existing_type=sa.Numeric(14, 2),
        nullable=False,
    )


def downgrade() -> None:
    # Revision 0049 introduced this column; 0050 only makes the existing
    # column idempotent/non-null. Leave it in place when downgrading to 0049.
    return None

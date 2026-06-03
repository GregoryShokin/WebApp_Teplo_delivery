"""add inventory swap groups

Revision ID: 0049_inventory_swap_groups
Revises: 0048_dds_seed
Create Date: 2026-06-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0049_inventory_swap_groups"
down_revision = "0048_dds_seed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inventory_audit_position",
        sa.Column("swap_group", sa.Text(), nullable=True, comment="source=app_managed"),
    )
    op.create_index(
        "ix_inventory_audit_position_swap_group",
        "inventory_audit_position",
        ["swap_group"],
    )
    op.add_column(
        "inventory_audit_item",
        sa.Column("amount", sa.Numeric(14, 2), nullable=True, comment="source=app_managed"),
    )
    op.execute("UPDATE inventory_audit_item SET amount = -shortage_amount WHERE amount IS NULL")
    op.alter_column("inventory_audit_item", "amount", nullable=False)
    op.add_column(
        "inventory_audit_item",
        sa.Column(
            "swap_group_override",
            sa.Text(),
            nullable=True,
            comment="source=app_managed",
        ),
    )


def downgrade() -> None:
    op.drop_column("inventory_audit_item", "swap_group_override")
    op.drop_column("inventory_audit_item", "amount")
    op.drop_index(
        "ix_inventory_audit_position_swap_group",
        table_name="inventory_audit_position",
    )
    op.drop_column("inventory_audit_position", "swap_group")

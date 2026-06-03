"""remap inventory audit items to positions

Revision ID: 0042_remap_inv_items_positions
Revises: 0041_inventory_position_rework
Create Date: 2026-06-02
"""

from __future__ import annotations

from alembic import op

revision = "0042_remap_inv_items_positions"
down_revision = "0041_inventory_position_rework"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE inventory_audit_item ai
        SET position_id = p.id
        FROM inventory_audit_position p
        WHERE ai.position_id IS NULL
          AND ai.iiko_product_guid IS NOT NULL
          AND ai.iiko_product_guid = p.iiko_product_guid
        """
    )


def downgrade() -> None:
    pass

"""rework inventory audit positions for iiko catalog sync

Revision ID: 0041_inventory_position_rework
Revises: 0040_inventory_audit
Create Date: 2026-06-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0041_inventory_position_rework"
down_revision = "0040_inventory_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM inventory_audit_position WHERE iiko_product_guid IS NULL")
    op.drop_constraint(
        "ck_inventory_audit_position_group",
        "inventory_audit_position",
        type_="check",
    )
    op.alter_column(
        "inventory_audit_position",
        "allocation_group",
        existing_type=sa.String(16),
        nullable=True,
    )
    op.execute(
        "UPDATE inventory_audit_position SET allocation_group='common' "
        "WHERE allocation_group='soy'"
    )
    op.create_check_constraint(
        "ck_inventory_audit_position_group",
        "inventory_audit_position",
        "allocation_group IS NULL OR allocation_group IN ('chefs','common','admins')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_inventory_audit_position_group",
        "inventory_audit_position",
        type_="check",
    )
    op.execute(
        "UPDATE inventory_audit_position SET allocation_group='soy' "
        "WHERE allocation_group='common'"
    )
    op.create_check_constraint(
        "ck_inventory_audit_position_group",
        "inventory_audit_position",
        "allocation_group IN ('chefs','soy','admins')",
    )
    op.alter_column(
        "inventory_audit_position",
        "allocation_group",
        existing_type=sa.String(16),
        nullable=False,
    )

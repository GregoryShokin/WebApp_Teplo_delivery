"""add inventory audit item exclusions

Revision ID: 0053_inv_item_exclusions
Revises: 0052_inv_employee_exclusions
Create Date: 2026-06-04
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0053_inv_item_exclusions"
down_revision = "0052_inv_employee_exclusions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inventory_audit_item_exclusion",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["audit_id"],
            ["inventory_audit.id"],
            name="fk_inventory_audit_item_exclusion_audit_id_inventory_audit",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["inventory_audit_item.id"],
            name="fk_inventory_audit_item_exclusion_item_id_inventory_audit_item",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name="fk_inventory_audit_item_exclusion_created_by_user_id_user",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "audit_id",
            "item_id",
            name="uq_inventory_audit_item_exclusion",
        ),
    )
    op.create_index(
        "ix_inventory_audit_item_exclusion_audit",
        "inventory_audit_item_exclusion",
        ["audit_id"],
    )
    op.create_index(
        "ix_inventory_audit_item_exclusion_item",
        "inventory_audit_item_exclusion",
        ["item_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inventory_audit_item_exclusion_item",
        table_name="inventory_audit_item_exclusion",
    )
    op.drop_index(
        "ix_inventory_audit_item_exclusion_audit",
        table_name="inventory_audit_item_exclusion",
    )
    op.drop_table("inventory_audit_item_exclusion")

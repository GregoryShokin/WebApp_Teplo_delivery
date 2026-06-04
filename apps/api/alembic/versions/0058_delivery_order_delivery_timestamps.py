"""add delivery taken and delivered timestamps

Revision ID: 0058_delivery_timestamps
Revises: 0057_courier_iiko_shift_matching
Create Date: 2026-06-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0058_delivery_timestamps"
down_revision = "0057_courier_iiko_shift_matching"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "delivery_order",
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "delivery_order",
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_delivery_order_courier_taken_at",
        "delivery_order",
        ["courier_iiko_id", "taken_at"],
    )
    op.execute(
        """
        update delivery_order
        set taken_at = on_way_at
        where taken_at is null and on_way_at is not null
        """
    )
    op.execute(
        """
        update delivery_order
        set delivered_at = closed_at
        where delivered_at is null and closed_at is not null
        """
    )


def downgrade() -> None:
    op.drop_index("ix_delivery_order_courier_taken_at", table_name="delivery_order")
    op.drop_column("delivery_order", "delivered_at")
    op.drop_column("delivery_order", "taken_at")

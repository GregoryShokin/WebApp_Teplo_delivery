"""add courier delivery order facts

Revision ID: 0021_courier_deliveries
Revises: 0020_cleanup_non_canon
Create Date: 2026-05-29
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0021_courier_deliveries"
down_revision = "0020_cleanup_non_canon"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "delivery_order",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("iiko_order_id", sa.Text(), nullable=False),
        sa.Column("order_number", sa.Text(), nullable=True),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("service_type", sa.Text(), nullable=True),
        sa.Column("courier_iiko_id", sa.Text(), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("on_way_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("way_duration_minutes", sa.Numeric(10, 2), nullable=True),
        sa.Column("revenue", sa.Numeric(12, 2), nullable=True),
        sa.Column(
            "raw",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
        sa.PrimaryKeyConstraint("id", name="pk_delivery_order"),
        sa.UniqueConstraint("iiko_order_id", name="uq_delivery_order_iiko_order_id"),
    )
    op.create_index("ix_delivery_order_work_date", "delivery_order", ["work_date"])
    op.create_index(
        "ix_delivery_order_courier_work_date",
        "delivery_order",
        ["courier_iiko_id", "work_date"],
    )
    op.create_index("ix_delivery_order_status", "delivery_order", ["status"])


def downgrade() -> None:
    op.drop_index("ix_delivery_order_status", table_name="delivery_order")
    op.drop_index("ix_delivery_order_courier_work_date", table_name="delivery_order")
    op.drop_index("ix_delivery_order_work_date", table_name="delivery_order")
    op.drop_table("delivery_order")

"""Управляемая разметка всей товарной номенклатуры iiko.

Revision ID: 0257_pnl_product_classification
Revises: 0256_pnl_iiko_goods_ledger
Create Date: 2026-08-04
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0257_pnl_product_classification"
down_revision = "0256_pnl_iiko_goods_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pnl_product_whitelist",
        sa.Column("source_kind", sa.String(24), nullable=True),
    )
    op.add_column(
        "pnl_product_whitelist",
        sa.Column("product_code", sa.String(64), nullable=True),
    )
    op.add_column(
        "pnl_product_whitelist",
        sa.Column("updated_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "pnl_product_whitelist",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_foreign_key(
        "fk_pnl_product_whitelist_updated_by_user",
        "pnl_product_whitelist",
        "user",
        ["updated_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        """
        UPDATE pnl_product_whitelist
        SET source_kind = CASE
            WHEN line_code IN ('packaging_inventory', 'pizza_box_inventory')
                THEN 'inventory'
            ELSE 'incoming_invoice'
        END
        """
    )
    op.alter_column("pnl_product_whitelist", "source_kind", nullable=False)
    op.alter_column("pnl_product_whitelist", "line_code", nullable=True)
    op.create_check_constraint(
        "ck_pnl_product_whitelist_source_kind",
        "pnl_product_whitelist",
        "source_kind in ('inventory', 'incoming_invoice')",
    )
    op.drop_index("uq_pnl_product_whitelist_guid_line", table_name="pnl_product_whitelist")
    op.create_index(
        "uq_pnl_product_whitelist_guid_source",
        "pnl_product_whitelist",
        ["iiko_product_guid", "source_kind"],
        unique=True,
    )

    op.create_table(
        "pnl_iiko_product_observation",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("source_kind", sa.String(24), nullable=False),
        sa.Column("iiko_product_guid", sa.String(64), nullable=False),
        sa.Column("product_name", sa.String(255), nullable=True),
        sa.Column("product_code", sa.String(64), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("rows_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "source_kind in ('inventory', 'incoming_invoice')",
            name="ck_pnl_iiko_product_observation_source_kind",
        ),
    )
    op.create_index(
        "uq_pnl_iiko_product_observation_slot",
        "pnl_iiko_product_observation",
        ["period_month", "source_kind", "iiko_product_guid"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_pnl_iiko_product_observation_slot",
        table_name="pnl_iiko_product_observation",
    )
    op.drop_table("pnl_iiko_product_observation")

    op.drop_index(
        "uq_pnl_product_whitelist_guid_source",
        table_name="pnl_product_whitelist",
    )
    op.create_index(
        "uq_pnl_product_whitelist_guid_line",
        "pnl_product_whitelist",
        ["iiko_product_guid", "line_code"],
        unique=True,
    )
    op.drop_constraint(
        "ck_pnl_product_whitelist_source_kind",
        "pnl_product_whitelist",
        type_="check",
    )
    op.execute(
        """
        UPDATE pnl_product_whitelist
        SET line_code = CASE
            WHEN source_kind = 'inventory' THEN 'packaging_inventory'
            ELSE 'aux_goods'
        END
        WHERE line_code IS NULL
        """
    )
    op.alter_column("pnl_product_whitelist", "line_code", nullable=False)
    op.drop_constraint(
        "fk_pnl_product_whitelist_updated_by_user",
        "pnl_product_whitelist",
        type_="foreignkey",
    )
    op.drop_column("pnl_product_whitelist", "updated_at")
    op.drop_column("pnl_product_whitelist", "updated_by_user_id")
    op.drop_column("pnl_product_whitelist", "product_code")
    op.drop_column("pnl_product_whitelist", "source_kind")

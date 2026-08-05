"""Детализация товарных строк ОПиУ по номенклатуре iiko.

Revision ID: 0256_pnl_iiko_goods_ledger
Revises: 0255_pnl_source_notes
Create Date: 2026-08-04
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0256_pnl_iiko_goods_ledger"
down_revision = "0255_pnl_source_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pnl_iiko_goods_fact",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("metric_code", sa.String(32), nullable=False),
        sa.Column(
            "line_code",
            sa.String(64),
            sa.ForeignKey("pnl_line.code", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_kind", sa.String(24), nullable=False),
        sa.Column("iiko_product_guid", sa.String(64), nullable=False),
        sa.Column("product_name", sa.String(255), nullable=True),
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
            name="ck_pnl_iiko_goods_fact_source_kind",
        ),
    )
    op.create_index(
        "uq_pnl_iiko_goods_fact_slot",
        "pnl_iiko_goods_fact",
        ["period_month", "metric_code", "source_kind", "iiko_product_guid"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_pnl_iiko_goods_fact_slot", table_name="pnl_iiko_goods_fact")
    op.drop_table("pnl_iiko_goods_fact")

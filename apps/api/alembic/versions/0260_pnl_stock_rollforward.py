"""Складской roll-forward ОПиУ и новая двухконтурная разметка товаров.

Revision ID: 0260_pnl_stock_rollforward
Revises: 0259_pnl_goods_auto_class
Create Date: 2026-08-04
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0260_pnl_stock_rollforward"
down_revision = "0259_pnl_goods_auto_class"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pnl_iiko_stock_fact",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("iiko_product_guid", sa.String(64), nullable=False),
        sa.Column("product_name", sa.String(255), nullable=True),
        sa.Column("product_code", sa.String(64), nullable=True),
        sa.Column("opening_quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("opening_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("receipts_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("closing_quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("closing_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("consumption_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("stores_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "uq_pnl_iiko_stock_fact_slot",
        "pnl_iiko_stock_fact",
        ["period_month", "iiko_product_guid"],
        unique=True,
    )

    op.drop_constraint(
        "ck_pnl_product_whitelist_status",
        "pnl_product_whitelist",
        type_="check",
    )
    op.execute(
        """
        UPDATE pnl_product_whitelist
        SET include_status = 'stocked', line_code = NULL
        WHERE include_status = 'product_inventory'
           OR (source_kind = 'inventory' AND include_status = 'include')
        """
    )
    op.create_check_constraint(
        "ck_pnl_product_whitelist_status",
        "pnl_product_whitelist",
        "include_status in ('include', 'requires_owner_review', 'exclude', 'stocked')",
    )

    op.execute(
        """
        UPDATE pnl_line
        SET title = 'Результат складского учёта',
            source_note = 'Фактический расход складских товаров (остаток на начало + '
                'приходы − остаток на конец) минус нормативный фудкост; штрафы по '
                'ревизиям уменьшают результат отдельным компонентом'
        WHERE code = 'audit_results'
        """
    )
    op.execute(
        """
        UPDATE pnl_line
        SET status = 'not_used',
            source_note = 'Заменено единым складским roll-forward с 01.07.2026'
        WHERE code IN ('packaging_inventory', 'pizza_box_inventory')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE pnl_line
        SET status = 'active'
        WHERE code IN ('packaging_inventory', 'pizza_box_inventory')
        """
    )
    op.execute(
        """
        UPDATE pnl_line
        SET title = 'Результаты ревизии'
        WHERE code = 'audit_results'
        """
    )
    op.drop_constraint(
        "ck_pnl_product_whitelist_status",
        "pnl_product_whitelist",
        type_="check",
    )
    op.execute(
        """
        UPDATE pnl_product_whitelist
        SET include_status = 'product_inventory'
        WHERE include_status = 'stocked'
        """
    )
    op.create_check_constraint(
        "ck_pnl_product_whitelist_status",
        "pnl_product_whitelist",
        "include_status in ('include', 'requires_owner_review', 'exclude', 'product_inventory')",
    )
    op.drop_index("uq_pnl_iiko_stock_fact_slot", table_name="pnl_iiko_stock_fact")
    op.drop_table("pnl_iiko_stock_fact")

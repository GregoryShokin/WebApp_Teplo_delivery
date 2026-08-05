"""allow product-inventory classification for PnL goods

Revision ID: 0259_pnl_goods_auto_class
Revises: 0258_pnl_product_monthly_workup
Create Date: 2026-08-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0259_pnl_goods_auto_class"
down_revision = "0258_pnl_product_monthly_workup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_pnl_product_whitelist_status",
        "pnl_product_whitelist",
        type_="check",
    )
    op.create_check_constraint(
        "ck_pnl_product_whitelist_status",
        "pnl_product_whitelist",
        "include_status in ('include', 'requires_owner_review', 'exclude', 'product_inventory')",
    )


def downgrade() -> None:
    # Защитный downgrade не должен падать на строках нового статуса: они безопасно становятся
    # обычным исключением из товарного расхода, после чего возвращается прежний constraint.
    op.execute(
        sa.text(
            "update pnl_product_whitelist set include_status = 'exclude' "
            "where include_status = 'product_inventory'"
        )
    )
    op.drop_constraint(
        "ck_pnl_product_whitelist_status",
        "pnl_product_whitelist",
        type_="check",
    )
    op.create_check_constraint(
        "ck_pnl_product_whitelist_status",
        "pnl_product_whitelist",
        "include_status in ('include', 'requires_owner_review', 'exclude')",
    )

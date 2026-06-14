"""Кэш номенклатуры iiko (iiko_product) для пикера накладных «Управление складом».

Очередь товаров из `/v2/entities/products/list` кэшируется в запрашиваемую таблицу
(поиск по имени/коду, фильтр по типу — пикер по умолчанию показывает GOODS) с
резолвнутой единицей измерения. Фаза 1 модуля «Накладные»: самодостаточна, ничего
существующего не трогает.

Revision ID: 0105_iiko_product_cache
Revises: 0104_courier_shift_day
Create Date: 2026-06-14
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0105_iiko_product_cache"
down_revision = "0104_courier_shift_day"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "iiko_product",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("iiko_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("code", sa.String(length=128), nullable=True),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("main_unit_guid", sa.String(length=64), nullable=True),
        sa.Column("unit", sa.String(length=16), nullable=True),
        sa.Column(
            "deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_iiko_product_iiko_id", "iiko_product", ["iiko_id"], unique=True)
    op.create_index("ix_iiko_product_type", "iiko_product", ["type"], unique=False)
    op.create_index(
        "ix_iiko_product_name_lower",
        "iiko_product",
        [sa.text("lower(name)")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_iiko_product_name_lower", table_name="iiko_product")
    op.drop_index("ix_iiko_product_type", table_name="iiko_product")
    op.drop_index("uq_iiko_product_iiko_id", table_name="iiko_product")
    op.drop_table("iiko_product")

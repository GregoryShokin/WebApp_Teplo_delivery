"""Накладные «Управление складом»: нормализованные строки + операционные поля.

Фаза 2 модуля «Накладные»: таблица invoice_line_item (строки с количеством, единицей,
флагом «персонал») и операционные колонки на supplier_invoice (дата+время чека для
автомэтча с банком, сумма персонал-строк, склад и статус пуша в iiko). Всё nullable/со
server_default — idempotent-синк iiko и существующие накладные не затрагиваются.

Revision ID: 0106_warehouse_invoice_lines
Revises: 0105_iiko_product_cache
Create Date: 2026-06-14
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0106_warehouse_invoice_lines"
down_revision = "0105_iiko_product_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "supplier_invoice",
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "supplier_invoice",
        sa.Column("staff_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
    )
    op.add_column(
        "supplier_invoice", sa.Column("store_guid", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "supplier_invoice",
        sa.Column(
            "iiko_push_status",
            sa.String(length=24),
            nullable=False,
            server_default="not_pushed",
        ),
    )
    op.add_column(
        "supplier_invoice",
        sa.Column("iiko_pushed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "supplier_invoice", sa.Column("iiko_push_error", sa.Text(), nullable=True)
    )
    op.create_index(
        "ix_supplier_invoice_issued_at", "supplier_invoice", ["issued_at"], unique=False
    )

    op.create_table(
        "invoice_line_item",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("iiko_product_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("product_guid", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("article", sa.String(length=128), nullable=True),
        sa.Column("unit", sa.String(length=16), nullable=True),
        sa.Column("quantity", sa.Numeric(14, 3), nullable=False),
        sa.Column("price", sa.Numeric(14, 2), nullable=False),
        sa.Column("sum", sa.Numeric(14, 2), nullable=False),
        sa.Column("vat_percent", sa.Numeric(5, 2), nullable=True),
        sa.Column("vat_sum", sa.Numeric(14, 2), nullable=True),
        sa.Column(
            "is_staff", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "sort_order", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"], ["supplier_invoice.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["iiko_product_id"], ["iiko_product.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_invoice_line_item_invoice", "invoice_line_item", ["invoice_id"], unique=False
    )
    op.create_index(
        "ix_invoice_line_item_product",
        "invoice_line_item",
        ["iiko_product_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_invoice_line_item_product", table_name="invoice_line_item")
    op.drop_index("ix_invoice_line_item_invoice", table_name="invoice_line_item")
    op.drop_table("invoice_line_item")
    op.drop_index("ix_supplier_invoice_issued_at", table_name="supplier_invoice")
    op.drop_column("supplier_invoice", "iiko_push_error")
    op.drop_column("supplier_invoice", "iiko_pushed_at")
    op.drop_column("supplier_invoice", "iiko_push_status")
    op.drop_column("supplier_invoice", "store_guid")
    op.drop_column("supplier_invoice", "staff_amount")
    op.drop_column("supplier_invoice", "issued_at")

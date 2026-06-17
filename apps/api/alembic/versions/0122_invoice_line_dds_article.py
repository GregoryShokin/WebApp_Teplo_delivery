"""Статья ДДС на позицию накладной/чека.

Чек местного закупа разносится в ДДС постро́чно (в одном чеке Магнита — и продукты,
и хозтовары), поэтому статья ДДС переезжает с уровня документа на уровень позиции
(``invoice_line_item.dds_article_id``). Nullable: у iiko-накладных позиций статьи нет.

Revision ID: 0122_invoice_line_dds_article
Revises: 0121_kassa_shortage_penalty
Create Date: 2026-06-18
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0122_invoice_line_dds_article"
down_revision = "0121_kassa_shortage_penalty"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoice_line_item",
        sa.Column("dds_article_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_invoice_line_item_dds_article",
        "invoice_line_item",
        "dds_articles",
        ["dds_article_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_invoice_line_item_dds_article", "invoice_line_item", type_="foreignkey"
    )
    op.drop_column("invoice_line_item", "dds_article_id")

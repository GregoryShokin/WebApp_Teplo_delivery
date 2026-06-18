"""Касса: трекинг изъятий (addPayOut) прочих расходов чека в iiko.

Таблица ``kassa_cheque_iiko_payout`` — барьер идемпотентности и аудит проводок-изъятий,
которыми не-складские расходы чека местного закупа дублируются в iiko. Проводки iiko
необратимы через API (нет delete/внесения), поэтому уникальность
(invoice_id, dds_article_id, source) не даёт задвоить уже проведённое при повторном прогоне.

Revision ID: 0123_kassa_cheque_iiko_payout
Revises: 0122_invoice_line_dds_article
Create Date: 2026-06-18
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0123_kassa_cheque_iiko_payout"
down_revision = "0122_invoice_line_dds_article"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kassa_cheque_iiko_payout",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dds_article_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("pay_out_type_id", sa.String(length=128), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("amount > 0", name="ck_cheque_iiko_payout_amount_positive"),
        sa.CheckConstraint("source in ('cash', 'card')", name="ck_cheque_iiko_payout_source"),
        sa.CheckConstraint("status in ('posted', 'failed')", name="ck_cheque_iiko_payout_status"),
        sa.ForeignKeyConstraint(["invoice_id"], ["supplier_invoice.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dds_article_id"], ["dds_articles.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "invoice_id",
            "dds_article_id",
            "source",
            name="uq_cheque_iiko_payout_invoice_article_source",
        ),
    )
    op.create_index(
        "ix_cheque_iiko_payout_invoice",
        "kassa_cheque_iiko_payout",
        ["invoice_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_cheque_iiko_payout_invoice", table_name="kassa_cheque_iiko_payout")
    op.drop_table("kassa_cheque_iiko_payout")

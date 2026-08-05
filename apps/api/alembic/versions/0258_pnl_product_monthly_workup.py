"""Месячная статья «Проработка» для временной товарной разметки.

Revision ID: 0258_pnl_product_monthly_workup
Revises: 0257_pnl_product_classification
Create Date: 2026-08-04
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0258_pnl_product_monthly_workup"
down_revision = "0257_pnl_product_classification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pnl_line = sa.table(
        "pnl_line",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("code", sa.String()),
        sa.column("title", sa.String()),
        sa.column("block", sa.String()),
        sa.column("kind", sa.String()),
        sa.column("sort_order", sa.Integer()),
        sa.column("level", sa.SmallInteger()),
        sa.column("sign_role", sa.SmallInteger()),
        sa.column("month_basis", sa.String()),
        sa.column("direction_aware", sa.Boolean()),
        sa.column("status", sa.String()),
        sa.column("source_note", sa.Text()),
    )
    op.bulk_insert(
        pnl_line,
        [
            {
                "id": uuid.uuid4(),
                "code": "goods_workup",
                "title": "Проработка",
                "block": "overhead",
                "kind": "source",
                "sort_order": 345,
                "level": 1,
                "sign_role": -1,
                "month_basis": "calendar",
                "direction_aware": False,
                "status": "active",
                "source_note": (
                    "Временная месячная разметка неизвестной номенклатуры iiko. "
                    "В следующем месяце товар снова требует постоянного решения"
                ),
            }
        ],
    )

    op.create_table(
        "pnl_product_monthly_decision",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("iiko_product_guid", sa.String(64), nullable=False),
        sa.Column("source_kind", sa.String(24), nullable=False),
        sa.Column(
            "decision_kind",
            sa.String(24),
            nullable=False,
            server_default="workup",
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "updated_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "source_kind in ('inventory', 'incoming_invoice')",
            name="ck_pnl_product_monthly_decision_source_kind",
        ),
        sa.CheckConstraint(
            "decision_kind = 'workup'",
            name="ck_pnl_product_monthly_decision_kind",
        ),
    )
    op.create_index(
        "uq_pnl_product_monthly_decision_slot",
        "pnl_product_monthly_decision",
        ["period_month", "iiko_product_guid"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_pnl_product_monthly_decision_slot",
        table_name="pnl_product_monthly_decision",
    )
    op.drop_table("pnl_product_monthly_decision")
    op.execute("DELETE FROM pnl_line WHERE code = 'goods_workup'")

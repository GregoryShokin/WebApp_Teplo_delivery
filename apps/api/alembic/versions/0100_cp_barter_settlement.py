"""barter loan settlement + invoice line items

Зачёт займов бартер-партнёров: накладные хранят позиции (`line_items`) для матчинга
по номенклатуре, а сведённые доходная↔расходная ссылаются на `barter_settlement`
(зачёт равных сумм с совпадающим набором товаров) и выпадают из открытого сальдо.

Revision ID: 0100_cp_barter_settlement
Revises: 0099_cp_relationship_direction
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0100_cp_barter_settlement"
down_revision = "0099_cp_relationship_direction"
branch_labels = None
depends_on = None

_SETTLEMENT = "barter_settlement"
_INVOICE = "supplier_invoice"


def upgrade() -> None:
    op.create_table(
        _SETTLEMENT,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("counterparty_id", UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "is_auto", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["counterparty_id"], ["counterparty.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_barter_settlement_cp", _SETTLEMENT, ["counterparty_id"])

    op.add_column(
        _INVOICE,
        sa.Column(
            "line_items",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        _INVOICE,
        sa.Column("barter_settlement_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_supplier_invoice_barter_settlement",
        _INVOICE,
        _SETTLEMENT,
        ["barter_settlement_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_supplier_invoice_barter_settlement", _INVOICE, ["barter_settlement_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_supplier_invoice_barter_settlement", table_name=_INVOICE)
    op.drop_constraint(
        "fk_supplier_invoice_barter_settlement", _INVOICE, type_="foreignkey"
    )
    op.drop_column(_INVOICE, "barter_settlement_id")
    op.drop_column(_INVOICE, "line_items")
    op.drop_index("ix_barter_settlement_cp", table_name=_SETTLEMENT)
    op.drop_table(_SETTLEMENT)

"""Явный бартер для складских накладных: роль, ссылка на заём, частичные возвраты.

Фаза 5 модуля «Накладные»: на supplier_invoice — явная роль (loan/return),
ссылка накладной-возврата на конкретный заём, кэш статуса возврата займа; новая
таблица barter_return_line для частичного возврата по товарам И суммам. Существующий
хроно-бартер (barter_role IS NULL) не затрагивается.

Revision ID: 0107_warehouse_barter
Revises: 0106_warehouse_invoice_lines
Create Date: 2026-06-15
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0107_warehouse_barter"
down_revision = "0106_warehouse_invoice_lines"
branch_labels = None
depends_on = None

_barter_role = postgresql.ENUM("loan", "return", name="supplier_invoice_barter_role")


def upgrade() -> None:
    _barter_role.create(op.get_bind(), checkfirst=True)
    op.add_column("supplier_invoice", sa.Column("barter_role", _barter_role, nullable=True))
    op.add_column(
        "supplier_invoice",
        sa.Column("barter_loan_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "supplier_invoice", sa.Column("barter_return_status", sa.String(length=24), nullable=True)
    )
    op.create_foreign_key(
        "fk_supplier_invoice_barter_loan",
        "supplier_invoice",
        "supplier_invoice",
        ["barter_loan_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_supplier_invoice_barter_loan", "supplier_invoice", ["barter_loan_id"], unique=False
    )

    op.create_table(
        "barter_return_line",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("return_invoice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("loan_invoice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("loan_line_item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("quantity", sa.Numeric(14, 3), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["return_invoice_id"], ["supplier_invoice.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["loan_invoice_id"], ["supplier_invoice.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["loan_line_item_id"], ["invoice_line_item.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_barter_return_loan", "barter_return_line", ["loan_invoice_id"], unique=False
    )
    op.create_index(
        "ix_barter_return_return", "barter_return_line", ["return_invoice_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_barter_return_return", table_name="barter_return_line")
    op.drop_index("ix_barter_return_loan", table_name="barter_return_line")
    op.drop_table("barter_return_line")
    op.drop_index("ix_supplier_invoice_barter_loan", table_name="supplier_invoice")
    op.drop_constraint("fk_supplier_invoice_barter_loan", "supplier_invoice", type_="foreignkey")
    op.drop_column("supplier_invoice", "barter_return_status")
    op.drop_column("supplier_invoice", "barter_loan_id")
    op.drop_column("supplier_invoice", "barter_role")
    _barter_role.drop(op.get_bind(), checkfirst=True)

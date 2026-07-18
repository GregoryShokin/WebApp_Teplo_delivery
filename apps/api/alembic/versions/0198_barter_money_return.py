"""Денежное гашение бартерного займа: строка гашения ← ДДС-проводка вместо возвратной накладной.

Целевой процесс владельца (2026-07-18): бартерный заём гасится либо ТОВАРОМ (возвратная
накладная, кг × исходная цена займа), либо ДЕНЬГАМИ (наш заём — контрагент платит по ТЕКУЩЕЙ
цене; их заём — мы платим «как обычную поставку»). Денежное гашение склад не двигает, поэтому
возвратной накладной у него нет — строка ссылается на реальную ДДС-проводку денег.

``return_invoice_id`` становится NULLABLE; добавляется ``cashflow_transaction_id`` (RESTRICT —
проводку, гасившую заём, нельзя удалить, не razобрав гашение). CHECK: строка несёт хотя бы один
источник (накладную или проводку).

Revision ID: 0198_barter_money_return
Revises: 0197_prepayment_bill_link
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0198_barter_money_return"
down_revision = "0197_prepayment_bill_link"
branch_labels = None
depends_on = None

_TABLE = "barter_return_line"


def upgrade() -> None:
    op.alter_column(_TABLE, "return_invoice_id", existing_type=sa.UUID(), nullable=True)
    op.add_column(
        _TABLE,
        sa.Column("cashflow_transaction_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_barter_return_cashflow_tx",
        _TABLE,
        "cashflow_transactions",
        ["cashflow_transaction_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_barter_return_cashflow_tx", _TABLE, ["cashflow_transaction_id"])
    op.create_check_constraint(
        "ck_barter_return_has_source",
        _TABLE,
        "(return_invoice_id IS NOT NULL) OR (cashflow_transaction_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_barter_return_has_source", _TABLE, type_="check")
    op.drop_index("ix_barter_return_cashflow_tx", table_name=_TABLE)
    op.drop_constraint("fk_barter_return_cashflow_tx", _TABLE, type_="foreignkey")
    # Денежные строки (без возвратной накладной) при откате теряют смысл — удаляем, иначе
    # NOT NULL не восстановится.
    op.execute(f"DELETE FROM {_TABLE} WHERE return_invoice_id IS NULL")  # noqa: S608
    op.drop_column(_TABLE, "cashflow_transaction_id")
    op.alter_column(_TABLE, "return_invoice_id", existing_type=sa.UUID(), nullable=False)

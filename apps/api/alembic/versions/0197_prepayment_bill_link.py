"""supplier_prepayment.bill_invoice_id — связь ДЗ ← оплаченный счёт (единый чокпоинт ДЗ/КЗ).

Канон владельца (2026-07-17): «оплата счёта уходит в дебиторскую задолженность». Единый
чокпоинт (reconcile_bill_prepayment в _recompute_status) заводит по оплаченному счёту
(doc_kind='bill') предоплату kind='prepaid_bill' и синхронизирует её с оплаченной суммой счёта.
Связь bill_invoice_id обеспечивает идемпотентность: одна ДЗ на счёт. Для прочих предоплат NULL.

Revision ID: 0197_prepayment_bill_link
Revises: 0196_supplier_invoice_doc_kind
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0197_prepayment_bill_link"
down_revision = "0196_supplier_invoice_doc_kind"
branch_labels = None
depends_on = None

_TABLE = "supplier_prepayment"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("bill_invoice_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_supplier_prepayment_bill_invoice",
        _TABLE,
        "supplier_invoice",
        ["bill_invoice_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Частичный УНИКАЛЬНЫЙ индекс: не более одной ДЗ на счёт (kind='prepaid_bill' — единственный,
    # кто ставит bill_invoice_id). Защищает идемпотентность чокпоинта на уровне БД от гонки двух
    # параллельных дверей гашения одного счёта (вторая вставка отклоняется → её транзакция ретраится
    # и видит существующую ДЗ). Служит и lookup-индексом.
    op.create_index(
        "uq_supplier_prepayment_bill",
        _TABLE,
        ["bill_invoice_id"],
        unique=True,
        postgresql_where=sa.text("bill_invoice_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_supplier_prepayment_bill", table_name=_TABLE)
    op.drop_constraint("fk_supplier_prepayment_bill_invoice", _TABLE, type_="foreignkey")
    op.drop_column(_TABLE, "bill_invoice_id")

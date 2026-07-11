"""Контур коррекции оплаченной накладной в iiko: id новой приходной.

Модель коррекции сменилась с «возврат дельты товаров» на «полный разворот»: возврат ВСЕХ старых
товаров (дебиторка поставщику) + НОВАЯ приходная на правильные товары БЕЗ оплаты (Y остаётся
«не оплачена» намеренно — зачёт завышает баланс поставщика). Нужен чек-пойнт id новой приходной
(Y), чтобы ретрай контура не пересоздавал её. Возврат живёт в iiko_return_external_id (0179).

Revision ID: 0180_inv_correction_new_ext
Revises: 0179_invoice_iiko_return
Create Date: 2026-07-11
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0180_inv_correction_new_ext"
down_revision = "0179_invoice_iiko_return"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "supplier_invoice",
        sa.Column("iiko_correction_new_external_id", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("supplier_invoice", "iiko_correction_new_external_id")

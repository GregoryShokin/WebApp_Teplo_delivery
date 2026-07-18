"""supplier_invoice.doc_kind (bill/closing) + activation_status для канона ДЗ/КЗ.

Канон владельца (2026-07-17): счёт — основание для платежа (bill), в баланс ДЗ/КЗ не входит;
УПД/акт/приходная — закрывающий документ (closing), гасит дебиторку / создаёт кредиторку.
activation_status обслуживает правило 4 (будущий УПД вступает в силу в свою дату).

Revision ID: 0196_supplier_invoice_doc_kind
Revises: 0195_profile_bank_prepayment
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0196_supplier_invoice_doc_kind"
down_revision = "0195_profile_bank_prepayment"
branch_labels = None
depends_on = None

_INVOICE = "supplier_invoice"
DOC_KIND = postgresql.ENUM("bill", "closing", name="supplier_invoice_doc_kind")


def upgrade() -> None:
    DOC_KIND.create(op.get_bind(), checkfirst=True)
    # server_default='closing': существующие строки по умолчанию — обязательства (текущее
    # поведение КЗ сохраняется), а не молча выпадают из кредиторки.
    op.add_column(
        _INVOICE,
        sa.Column(
            "doc_kind",
            postgresql.ENUM(name="supplier_invoice_doc_kind", create_type=False),
            nullable=False,
            server_default="closing",
        ),
    )
    op.add_column(
        _INVOICE,
        sa.Column(
            "activation_status",
            sa.String(16),
            nullable=False,
            server_default="active",
        ),
    )
    # Backfill: неоплаченные СЧЕТА (не долг) → bill, уходят из КЗ. Почтовые счета (Лема,
    # ДоксИнБокс, АЙКО) и СБИС-СчетВх материализуются в supplier_invoice, но по канону это
    # очередь оплат, а не кредиторка. Частично/полностью оплаченные почтовые (ЭкоЦентр 837
    # partially_paid) остаются closing — владелец платит по ним как по обязательству, ручную
    # перевязку стенда не двигаем. iiko/касса/склад/УПД/ручной реестр — closing (дефолт).
    # source::text, а не прямое сравнение с enum-литералом: значение 'sbis' добавлено миграцией
    # 0193 и на свежей БД живёт в том же alembic-батче, а PG запрещает использовать новое
    # значение enum в одной транзакции с ALTER TYPE ADD VALUE (UnsafeNewEnumValueUsageError).
    op.execute(
        "UPDATE supplier_invoice SET doc_kind = 'bill' "
        "WHERE payment_status = 'unpaid' AND ("
        "source::text = 'email' "
        "OR (source::text = 'sbis' AND raw_payload->>'doc_type' = 'СчетВх')"
        ")"
    )
    op.create_index("ix_supplier_invoice_doc_kind", _INVOICE, ["doc_kind"])


def downgrade() -> None:
    op.drop_index("ix_supplier_invoice_doc_kind", table_name=_INVOICE)
    op.drop_column(_INVOICE, "activation_status")
    op.drop_column(_INVOICE, "doc_kind")
    DOC_KIND.drop(op.get_bind(), checkfirst=True)

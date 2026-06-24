"""Предоплаты поставщикам (дебиторка) + гашение накладных из предоплаты.

Заводим отдельный леджер выданных авансов поставщику ``supplier_prepayment`` (мы заплатили
вперёд — поставщик должен поставку). Предоплата создаётся реальным расходом денег
(out-CashflowTransaction, ``source_kind='supplier_prepayment'``), обычно с «Сейфа» или
банк-черновиком по реквизитам; гасится приходящими payable-накладными через
``InvoicePaymentAllocation(source_kind='prepayment', prepayment_id=...)``, которая денег НЕ
двигает. Это отдельный учёт от кредиторки (неоплаченные накладные) и товарного бартера.

Добавляем:
* таблицу ``supplier_prepayment``;
* в ``invoice_payment_allocation`` — колонку ``prepayment_id`` (источник гашения) и обновляем
  CHECK ``ck_invoice_allocation_single_source`` до «не более одного источника из трёх»;
* в enum ``invoice_allocation_source`` — значение ``prepayment``;
* в ``counterparty_payment_draft`` — флаг ``creates_prepayment`` и ``prepayment_article_id``
  (standalone-черновик «банк по реквизитам» без накладной создаёт предоплату при оплате);
* статью ДДС «Авансы поставщикам» (``advance_to_supplier``).

Revision ID: 0140_supplier_prepayment
Revises: 0139_advance_bank_draft
Create Date: 2026-06-25
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0140_supplier_prepayment"
down_revision = "0139_advance_bank_draft"
branch_labels = None
depends_on = None

_OLD_ALLOC_CHECK = "not (bank_operation_id is not null and cashflow_transaction_id is not null)"
_NEW_ALLOC_CHECK = (
    "((bank_operation_id is not null)::int + (cashflow_transaction_id is not null)::int "
    "+ (prepayment_id is not null)::int) <= 1"
)


def upgrade() -> None:
    # 1) Леджер выданных предоплат поставщикам.
    op.create_table(
        "supplier_prepayment",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("counterparty_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("wallet_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("amount_settled", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("cashflow_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("article_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_supplier_prepayment_amount_positive"),
        sa.CheckConstraint(
            "amount_settled >= 0 and amount_settled <= amount",
            name="ck_supplier_prepayment_settled_range",
        ),
        sa.ForeignKeyConstraint(
            ["counterparty_id"], ["counterparty.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallet.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["cashflow_transaction_id"], ["cashflow_transactions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["article_id"], ["dds_articles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_supplier_prepayment_counterparty", "supplier_prepayment", ["counterparty_id"]
    )
    op.create_index("ix_supplier_prepayment_status", "supplier_prepayment", ["status"])

    # 2) Источник гашения «из предоплаты» в аллокации + обновлённый CHECK (≤1 источник из трёх).
    op.add_column(
        "invoice_payment_allocation",
        sa.Column("prepayment_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_invoice_allocation_prepayment",
        "invoice_payment_allocation",
        "supplier_prepayment",
        ["prepayment_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_constraint(
        "ck_invoice_allocation_single_source", "invoice_payment_allocation", type_="check"
    )
    op.create_check_constraint(
        "ck_invoice_allocation_single_source", "invoice_payment_allocation", _NEW_ALLOC_CHECK
    )

    # 3) Значение enum для аллокации-гашения из предоплаты (не используется в этой же транзакции).
    op.execute("ALTER TYPE invoice_allocation_source ADD VALUE IF NOT EXISTS 'prepayment'")

    # 4) Standalone-черновик «банк по реквизитам» → создаёт предоплату при оплате.
    op.add_column(
        "counterparty_payment_draft",
        sa.Column(
            "creates_prepayment",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "counterparty_payment_draft",
        sa.Column("prepayment_article_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_counterparty_payment_draft_prepay_article",
        "counterparty_payment_draft",
        "dds_articles",
        ["prepayment_article_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # 5) Статья ДДС «Авансы поставщикам» (дебиторка предоплат).
    op.execute(
        """
        INSERT INTO dds_articles (id, code, name, movement_type, activity_type, is_active)
        VALUES (gen_random_uuid(), 'advance_to_supplier',
                'Авансы поставщикам', 'outflow', 'operating', true)
        ON CONFLICT (code) DO UPDATE SET is_active = true
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM dds_articles WHERE code = 'advance_to_supplier'")

    op.drop_constraint(
        "fk_counterparty_payment_draft_prepay_article",
        "counterparty_payment_draft",
        type_="foreignkey",
    )
    op.drop_column("counterparty_payment_draft", "prepayment_article_id")
    op.drop_column("counterparty_payment_draft", "creates_prepayment")

    # Вернуть CHECK к виду «не оба денежных источника сразу» и убрать колонку prepayment_id.
    op.drop_constraint(
        "ck_invoice_allocation_single_source", "invoice_payment_allocation", type_="check"
    )
    op.create_check_constraint(
        "ck_invoice_allocation_single_source", "invoice_payment_allocation", _OLD_ALLOC_CHECK
    )
    op.drop_constraint(
        "fk_invoice_allocation_prepayment", "invoice_payment_allocation", type_="foreignkey"
    )
    op.drop_column("invoice_payment_allocation", "prepayment_id")

    op.drop_index("ix_supplier_prepayment_status", table_name="supplier_prepayment")
    op.drop_index("ix_supplier_prepayment_counterparty", table_name="supplier_prepayment")
    op.drop_table("supplier_prepayment")
    # Значение enum 'prepayment' не удаляем — PostgreSQL не поддерживает DROP VALUE.

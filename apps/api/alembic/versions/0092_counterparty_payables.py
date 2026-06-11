"""counterparty payables module

Операционный контур «Контрагенты»: счета-обязательства (supplier_invoice),
профиль контрагента (counterparty_payable_profile), черновики платежей в банк
(counterparty_payment_draft) и аллокации платёж↔накладная (invoice_payment_allocation),
плюс справочник леджер-категорий. Отдельно от платёжного календаря (план) и УДКЗ
(остатки): это inbox «100% надо заплатить», который питает банковский черновик.

Revision ID: 0092_counterparty_payables
Revises: 0091_dds_employee_advance
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0092_counterparty_payables"
down_revision = "0091_dds_employee_advance"
branch_labels = None
depends_on = None

_LEDGER = "counterparty_ledger_category"
_PROFILE = "counterparty_payable_profile"
_DRAFT = "counterparty_payment_draft"
_INVOICE = "supplier_invoice"
_ALLOCATION = "invoice_payment_allocation"

INVOICE_SOURCE = postgresql.ENUM(
    "iiko", "manual", "email", "telegram", name="counterparty_invoice_source"
)
INVOICE_STATUS = postgresql.ENUM(
    "unpaid", "partially_paid", "paid", "void", name="counterparty_invoice_status"
)
ALLOCATION_SOURCE = postgresql.ENUM("bank", "cash", name="invoice_allocation_source")


def upgrade() -> None:
    bind = op.get_bind()
    for enum in (INVOICE_SOURCE, INVOICE_STATUS, ALLOCATION_SOURCE):
        enum.create(bind, checkfirst=True)

    op.create_table(
        _LEDGER,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("code", name="uq_counterparty_ledger_category_code"),
    )

    op.create_table(
        _PROFILE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("counterparty_id", UUID(as_uuid=True), nullable=False),
        sa.Column("ledger_category_id", UUID(as_uuid=True), nullable=True),
        sa.Column("brand_group", sa.String(length=160), nullable=True),
        sa.Column("internal_name", sa.String(length=255), nullable=True),
        sa.Column("payment_delay_days", sa.Integer(), nullable=True),
        sa.Column(
            "requisites",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "requisites_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("requisites_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requisites_verified_by_user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False, server_default="active"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["counterparty_id"], ["counterparty.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["ledger_category_id"],
            ["counterparty_ledger_category.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["requisites_verified_by_user_id"], ["user.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("counterparty_id", name="uq_counterparty_payable_profile_cp"),
    )

    op.create_table(
        _DRAFT,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("counterparty_id", UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("provider_ref", sa.Text(), nullable=True),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["counterparty_id"], ["counterparty.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "status in ('created', 'updated', 'paid', 'failed')",
            name="ck_counterparty_payment_draft_status",
        ),
    )
    op.create_index("ix_counterparty_payment_draft_cp", _DRAFT, ["counterparty_id"])

    op.create_table(
        _INVOICE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("counterparty_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "source",
            postgresql.ENUM(name="counterparty_invoice_source", create_type=False),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("number", sa.String(length=128), nullable=True),
        sa.Column("invoice_date", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "payment_status",
            postgresql.ENUM(name="counterparty_invoice_status", create_type=False),
            nullable=False,
            server_default="unpaid",
        ),
        sa.Column("draft_id", UUID(as_uuid=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("raw_payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["counterparty_id"], ["counterparty.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["draft_id"], ["counterparty_payment_draft.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "uq_supplier_invoice_source_external",
        _INVOICE,
        ["source", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )
    op.create_index("ix_supplier_invoice_cp", _INVOICE, ["counterparty_id"])
    op.create_index("ix_supplier_invoice_status", _INVOICE, ["payment_status"])
    op.create_index("ix_supplier_invoice_draft", _INVOICE, ["draft_id"])

    op.create_table(
        _ALLOCATION,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("invoice_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "source_kind",
            postgresql.ENUM(name="invoice_allocation_source", create_type=False),
            nullable=False,
        ),
        sa.Column("bank_operation_id", UUID(as_uuid=True), nullable=True),
        sa.Column("cashflow_transaction_id", UUID(as_uuid=True), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["invoice_id"], ["supplier_invoice.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["bank_operation_id"], ["bank_operations.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["cashflow_transaction_id"], ["cashflow_transactions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "not (bank_operation_id is not null and cashflow_transaction_id is not null)",
            name="ck_invoice_allocation_single_source",
        ),
    )
    op.create_index("ix_invoice_allocation_invoice", _ALLOCATION, ["invoice_id"])
    op.create_index("ix_invoice_allocation_bank_op", _ALLOCATION, ["bank_operation_id"])


def downgrade() -> None:
    op.drop_table(_ALLOCATION)
    op.drop_table(_INVOICE)
    op.drop_index("ix_counterparty_payment_draft_cp", table_name=_DRAFT)
    op.drop_table(_DRAFT)
    op.drop_table(_PROFILE)
    op.drop_table(_LEDGER)
    bind = op.get_bind()
    for enum in (ALLOCATION_SOURCE, INVOICE_STATUS, INVOICE_SOURCE):
        enum.drop(bind, checkfirst=True)

"""add dds classification tables

Revision ID: 0047_dds_classification
Revises: 0046_dds_core_schema
Create Date: 2026-06-03
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0047_dds_classification"
down_revision = "0046_dds_core_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transfer_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("from_wallet_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("to_wallet_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("initiated_at", sa.Date(), nullable=False),
        sa.Column("completed_at", sa.Date(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
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
        sa.ForeignKeyConstraint(
            ["from_wallet_id"],
            ["wallet.id"],
            name="fk_transfer_groups_from_wallet_id_wallet",
        ),
        sa.ForeignKeyConstraint(
            ["to_wallet_id"],
            ["wallet.id"],
            name="fk_transfer_groups_to_wallet_id_wallet",
        ),
    )

    op.create_table(
        "cashflow_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("wallet_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("operation_date", sa.Date(), nullable=False),
        sa.Column("article_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("counterparty_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("transfer_group_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payment_purpose", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "quality_status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'auto'"),
        ),
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
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["dds_articles.id"],
            name="fk_cashflow_transactions_article_id_dds_articles",
        ),
        sa.ForeignKeyConstraint(
            ["counterparty_id"],
            ["counterparty.id"],
            name="fk_cashflow_transactions_counterparty_id_counterparty",
        ),
        sa.ForeignKeyConstraint(
            ["transfer_group_id"],
            ["transfer_groups.id"],
            name="fk_cashflow_transactions_transfer_group_id_transfer_groups",
        ),
        sa.ForeignKeyConstraint(
            ["wallet_id"],
            ["wallet.id"],
            name="fk_cashflow_transactions_wallet_id_wallet",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_cashflow_transactions_operation_date",
        "cashflow_transactions",
        ["operation_date"],
    )
    op.create_index("ix_cashflow_transactions_wallet_id", "cashflow_transactions", ["wallet_id"])
    op.create_index("ix_cashflow_transactions_article_id", "cashflow_transactions", ["article_id"])

    op.create_table(
        "bank_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_operation_id", sa.String(length=128), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("operation_date", sa.Date(), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default=sa.text("'RUB'")),
        sa.Column("counterparty_name_raw", sa.String(length=512), nullable=True),
        sa.Column("counterparty_inn_raw", sa.String(length=20), nullable=True),
        sa.Column("counterparty_account_raw", sa.String(length=40), nullable=True),
        sa.Column("payment_purpose", sa.Text(), nullable=True),
        sa.Column("document_number", sa.String(length=64), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "classification_status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("cashflow_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["account.id"],
            name="fk_bank_operations_account_id_account",
        ),
        sa.ForeignKeyConstraint(
            ["cashflow_transaction_id"],
            ["cashflow_transactions.id"],
            name="fk_bank_operations_cashflow_tx_id",
        ),
    )
    op.create_index(
        "uq_bank_operations_provider_operation",
        "bank_operations",
        ["provider", "provider_operation_id"],
        unique=True,
    )
    op.create_index("ix_bank_operations_operation_date", "bank_operations", ["operation_date"])
    op.create_index(
        "ix_bank_operations_classification_status",
        "bank_operations",
        ["classification_status"],
    )

    op.create_table(
        "classification_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("direction", sa.String(length=8), nullable=True),
        sa.Column("counterparty_inn_match", sa.String(length=12), nullable=True),
        sa.Column("counterparty_name_pattern", sa.String(length=255), nullable=True),
        sa.Column("purpose_pattern", sa.String(length=255), nullable=True),
        sa.Column("amount_min", sa.Numeric(18, 2), nullable=True),
        sa.Column("amount_max", sa.Numeric(18, 2), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("article_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("counterparty_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["dds_articles.id"],
            name="fk_classification_rules_article_id_dds_articles",
        ),
        sa.ForeignKeyConstraint(
            ["counterparty_id"],
            ["counterparty.id"],
            name="fk_classification_rules_counterparty_id_counterparty",
        ),
    )

    op.create_table(
        "wallet_balance_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("wallet_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("balance", sa.Numeric(18, 2), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["wallet_id"],
            ["wallet.id"],
            name="fk_wallet_balance_snapshots_wallet_id_wallet",
        ),
    )
    op.create_index(
        "uq_wallet_balance_snapshots_wallet_date_source",
        "wallet_balance_snapshots",
        ["wallet_id", "snapshot_date", "source"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_wallet_balance_snapshots_wallet_date_source",
        table_name="wallet_balance_snapshots",
    )
    op.drop_table("wallet_balance_snapshots")
    op.drop_table("classification_rules")
    op.drop_index("ix_bank_operations_classification_status", table_name="bank_operations")
    op.drop_index("ix_bank_operations_operation_date", table_name="bank_operations")
    op.drop_index("uq_bank_operations_provider_operation", table_name="bank_operations")
    op.drop_table("bank_operations")
    op.drop_index("ix_cashflow_transactions_article_id", table_name="cashflow_transactions")
    op.drop_index("ix_cashflow_transactions_wallet_id", table_name="cashflow_transactions")
    op.drop_index("ix_cashflow_transactions_operation_date", table_name="cashflow_transactions")
    op.drop_table("cashflow_transactions")
    op.drop_table("transfer_groups")

"""add dds core schema

Revision ID: 0046_dds_core_schema
Revises: 0045_substitute_assignments
Create Date: 2026-06-03
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0046_dds_core_schema"
down_revision = "0045_substitute_assignments"
branch_labels = None
depends_on = None


WALLET_TYPE_VALUES = ("bank", "cash_register", "cash_safe", "store_cash", "reserve")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for value in WALLET_TYPE_VALUES:
            op.execute(sa.text(f"ALTER TYPE wallet_type ADD VALUE IF NOT EXISTS '{value}'"))

    op.add_column("source_credential", sa.Column("provider", sa.String(length=32), nullable=True))
    op.add_column(
        "source_credential",
        sa.Column("credential_kind", sa.String(length=64), nullable=True),
    )
    op.add_column("source_credential", sa.Column("value_encrypted", sa.Text(), nullable=True))
    op.add_column(
        "source_credential",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "source_credential",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column(
        "source_credential",
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "source_credential",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.add_column(
        "source_credential",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE source_credential sc
            SET
                provider = COALESCE(ds.code, 'legacy'),
                credential_kind = 'vault_key',
                value_encrypted = COALESCE(sc.vault_key, ''),
                is_active = (sc.status = 'active')
            FROM data_source ds
            WHERE sc.data_source_id = ds.id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE source_credential
            SET
                provider = COALESCE(provider, 'legacy'),
                credential_kind = COALESCE(credential_kind, 'vault_key'),
                value_encrypted = COALESCE(value_encrypted, COALESCE(vault_key, '')),
                is_active = (status = 'active')
            """
        )
    )
    op.alter_column("source_credential", "provider", nullable=False)
    op.alter_column("source_credential", "credential_kind", nullable=False)
    op.alter_column("source_credential", "value_encrypted", nullable=False)
    op.alter_column("source_credential", "data_source_id", nullable=True)
    op.alter_column("source_credential", "vault_key", nullable=True)
    op.create_index(
        "uq_source_credential_active_kind",
        "source_credential",
        ["provider", "credential_kind"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )

    op.create_table(
        "account",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("bank_code", sa.String(length=32), nullable=False),
        sa.Column("account_number", sa.String(length=40), nullable=True),
        sa.Column("bic", sa.String(length=20), nullable=True),
        sa.Column("legal_entity", sa.String(length=255), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default=sa.text("'RUB'")),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("opened_at", sa.Date(), nullable=True),
        sa.Column("closed_at", sa.Date(), nullable=True),
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
    )
    op.create_index(
        "uq_account_bank_code_account_number",
        "account",
        ["bank_code", "account_number"],
        unique=True,
        postgresql_where=sa.text("account_number IS NOT NULL"),
    )

    op.add_column("wallet", sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column(
        "wallet",
        sa.Column(
            "opening_balance",
            sa.Numeric(18, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column("wallet", sa.Column("opening_balance_date", sa.Date(), nullable=True))
    op.create_foreign_key(
        "fk_wallet_account_id_account",
        "wallet",
        "account",
        ["account_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "own_accounts_registry",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("bank_code", sa.String(length=32), nullable=False),
        sa.Column("account_number", sa.String(length=40), nullable=False),
        sa.Column("bic", sa.String(length=20), nullable=True),
        sa.Column("legal_entity_inn", sa.String(length=12), nullable=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["account.id"],
            name="fk_own_accounts_registry_account_id_account",
        ),
    )
    op.create_index(
        "uq_own_accounts_registry_bank_account",
        "own_accounts_registry",
        ["bank_code", "account_number"],
        unique=True,
    )

    op.create_table(
        "dds_articles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("movement_type", sa.String(length=16), nullable=False),
        sa.Column("activity_type", sa.String(length=16), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("description", sa.Text(), nullable=True),
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
            ["parent_id"],
            ["dds_articles.id"],
            name="fk_dds_articles_parent_id_dds_articles",
        ),
        sa.UniqueConstraint("code", name="uq_dds_articles_code"),
    )

    op.create_table(
        "dds_article_aliases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("article_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("alias", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["dds_articles.id"],
            name="fk_dds_article_aliases_article_id_dds_articles",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "uq_dds_article_aliases_alias_ci",
        "dds_article_aliases",
        [sa.text("lower(alias)")],
        unique=True,
    )

    op.create_table(
        "counterparty_alias",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("counterparty_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("alias", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(
            ["counterparty_id"],
            ["counterparty.id"],
            name="fk_counterparty_alias_counterparty_id_counterparty",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "uq_counterparty_alias_alias_ci",
        "counterparty_alias",
        [sa.text("lower(alias)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_counterparty_alias_alias_ci", table_name="counterparty_alias")
    op.drop_table("counterparty_alias")
    op.drop_index("uq_dds_article_aliases_alias_ci", table_name="dds_article_aliases")
    op.drop_table("dds_article_aliases")
    op.drop_table("dds_articles")
    op.drop_index("uq_own_accounts_registry_bank_account", table_name="own_accounts_registry")
    op.drop_table("own_accounts_registry")
    op.drop_constraint("fk_wallet_account_id_account", "wallet", type_="foreignkey")
    op.drop_column("wallet", "opening_balance_date")
    op.drop_column("wallet", "opening_balance")
    op.drop_column("wallet", "account_id")
    op.drop_index("uq_account_bank_code_account_number", table_name="account")
    op.drop_table("account")

    op.drop_index("uq_source_credential_active_kind", table_name="source_credential")
    op.execute(sa.text("DELETE FROM source_credential WHERE data_source_id IS NULL"))
    op.alter_column("source_credential", "vault_key", nullable=False)
    op.alter_column("source_credential", "data_source_id", nullable=False)
    op.drop_column("source_credential", "updated_at")
    op.drop_column("source_credential", "created_at")
    op.drop_column("source_credential", "metadata")
    op.drop_column("source_credential", "is_active")
    op.drop_column("source_credential", "expires_at")
    op.drop_column("source_credential", "value_encrypted")
    op.drop_column("source_credential", "credential_kind")
    op.drop_column("source_credential", "provider")

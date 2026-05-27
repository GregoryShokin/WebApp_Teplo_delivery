"""core domain, audit and settings schema

Revision ID: 0001_core_domain
Revises:
Create Date: 2026-05-27
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001_core_domain"
down_revision = None
branch_labels = None
depends_on = None


LOCATION_STATUS = postgresql.ENUM("active", "inactive", name="location_status")
COUNTERPARTY_TYPE = postgresql.ENUM(
    "legal_entity",
    "individual",
    "bank",
    "tax_authority",
    name="counterparty_type",
)
COUNTERPARTY_ROLE = postgresql.ENUM(
    "supplier",
    "customer",
    "bank",
    "employee",
    "owner",
    "tax_authority",
    "partner",
    name="counterparty_role_type",
)
WALLET_TYPE = postgresql.ENUM("bank_account", "cash", "fund", "deposit", name="wallet_type")
PERIOD_TYPE = postgresql.ENUM("month", "week", "day", name="period_type")
PERIOD_STATUS = postgresql.ENUM("open", "closed", "finalized", name="period_status")
DATA_SOURCE_TYPE = postgresql.ENUM(
    "api",
    "sheets",
    "paper_ocr",
    "manual",
    "ai_email",
    "browser_lk",
    name="data_source_type",
)
PARSED_DOCUMENT_STATUS = postgresql.ENUM(
    "extracted",
    "auto_confirmed",
    "needs_review",
    "rejected",
    name="parsed_document_status",
)
QUALITY_STATUS = postgresql.ENUM(
    "draft",
    "partial",
    "final",
    "requires_review",
    "not_applicable",
    name="quality_status",
)


def _create_enums() -> None:
    bind = op.get_bind()
    for enum in (
        LOCATION_STATUS,
        COUNTERPARTY_TYPE,
        COUNTERPARTY_ROLE,
        WALLET_TYPE,
        PERIOD_TYPE,
        PERIOD_STATUS,
        DATA_SOURCE_TYPE,
        PARSED_DOCUMENT_STATUS,
        QUALITY_STATUS,
    ):
        enum.create(bind, checkfirst=True)


def _drop_enums() -> None:
    bind = op.get_bind()
    for enum in (
        QUALITY_STATUS,
        PARSED_DOCUMENT_STATUS,
        DATA_SOURCE_TYPE,
        PERIOD_STATUS,
        PERIOD_TYPE,
        WALLET_TYPE,
        COUNTERPARTY_ROLE,
        COUNTERPARTY_TYPE,
        LOCATION_STATUS,
    ):
        enum.drop(bind, checkfirst=True)


def upgrade() -> None:
    _create_enums()

    op.create_table(
        "organization",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("inn", sa.String(length=12), nullable=True),
        sa.Column("kpp", sa.String(length=9), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "user",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("email", name="uq_user_email"),
    )

    op.create_table(
        "role",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.UniqueConstraint("code", name="uq_role_code"),
    )

    op.create_table(
        "location",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("address", sa.String(length=512), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(name="location_status", create_type=False),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name="fk_location_organization_id_organization",
            ondelete="RESTRICT",
        ),
    )

    op.create_table(
        "user_role",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name="fk_user_role_organization_id_organization",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["role.id"],
            name="fk_user_role_role_id_role",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_user_role_user_id_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("user_id", "role_id", "organization_id", name="pk_user_role"),
    )

    op.create_table(
        "counterparty",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("inn", sa.String(length=12), nullable=True),
        sa.Column(
            "type",
            postgresql.ENUM(name="counterparty_type", create_type=False),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "uq_counterparty_inn_not_null",
        "counterparty",
        ["inn"],
        unique=True,
        postgresql_where=sa.text("inn IS NOT NULL"),
    )

    op.create_table(
        "counterparty_role",
        sa.Column("counterparty_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM(name="counterparty_role_type", create_type=False),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["counterparty_id"],
            ["counterparty.id"],
            name="fk_counterparty_role_counterparty_id_counterparty",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("counterparty_id", "role", name="pk_counterparty_role"),
    )

    op.create_table(
        "employee",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "full_name",
            sa.String(length=255),
            nullable=False,
            comment="source=iiko; read-only in app",
        ),
        sa.Column("iiko_id", sa.String(length=128), nullable=False),
        sa.Column(
            "position", sa.String(length=160), nullable=True, comment="source=app_managed"
        ),
        sa.Column(
            "category", sa.String(length=160), nullable=True, comment="source=app_managed"
        ),
        sa.Column(
            "status", sa.String(length=64), nullable=False, comment="source=app_managed"
        ),
        sa.Column("hire_date", sa.Date(), nullable=True, comment="source=app_managed"),
        sa.Column("fire_date", sa.Date(), nullable=True, comment="source=app_managed"),
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
        sa.UniqueConstraint("iiko_id", name="uq_employee_iiko_id"),
    )

    op.create_table(
        "wallet",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column(
            "type",
            postgresql.ENUM(name="wallet_type", create_type=False),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "is_internal_transfer_eligible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("code", name="uq_wallet_code"),
    )

    op.create_table(
        "period",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "period_type",
            postgresql.ENUM(name="period_type", create_type=False),
            nullable=False,
        ),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="period_status", create_type=False),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
    )
    op.create_index(
        "uq_period_type_start_end",
        "period",
        ["period_type", "start_date", "end_date"],
        unique=True,
    )

    op.create_table(
        "data_source",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column(
            "type",
            postgresql.ENUM(name="data_source_type", create_type=False),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("code", name="uq_data_source_code"),
    )

    op.create_table(
        "source_credential",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("data_source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vault_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("last_rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["data_source_id"],
            ["data_source.id"],
            name="fk_source_credential_data_source_id_data_source",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "source_snapshot",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("data_source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("content_hash", sa.String(length=128), nullable=False),
        sa.Column("ref_path", sa.String(length=512), nullable=False),
        sa.ForeignKeyConstraint(
            ["data_source_id"],
            ["data_source.id"],
            name="fk_source_snapshot_data_source_id_data_source",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "parsed_document",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("source_snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="parsed_document_status", create_type=False),
            nullable=False,
            server_default=sa.text("'extracted'"),
        ),
        sa.Column(
            "extracted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.ForeignKeyConstraint(
            ["source_snapshot_id"],
            ["source_snapshot.id"],
            name="fk_parsed_document_source_snapshot_id_source_snapshot",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "source_document",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("parsed_document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("confirmed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("document_type", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["confirmed_by_user_id"],
            ["user.id"],
            name="fk_source_document_confirmed_by_user_id_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parsed_document_id"],
            ["parsed_document.id"],
            name="fk_source_document_parsed_document_id_parsed_document",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("parsed_document_id", name="uq_source_document_parsed_document_id"),
    )

    op.create_table(
        "agent_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("agent_name", sa.String(length=128), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "result",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    op.create_table(
        "agent_action",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("target_table", sa.String(length=128), nullable=True),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("before_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_run_id"],
            ["agent_run.id"],
            name="fk_agent_action_agent_run_id_agent_run",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "app_setting",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("key", sa.String(length=160), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("value_type", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["user.id"],
            name="fk_app_setting_updated_by_user_id_user",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("key", name="uq_app_setting_key"),
    )

    op.create_table(
        "app_setting_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("setting_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("old_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new_value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("changed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["changed_by_user_id"],
            ["user.id"],
            name="fk_app_setting_history_changed_by_user_id_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["setting_id"],
            ["app_setting.id"],
            name="fk_app_setting_history_setting_id_app_setting",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("app_setting_history")
    op.drop_table("app_setting")
    op.drop_table("agent_action")
    op.drop_table("agent_run")
    op.drop_table("source_document")
    op.drop_table("parsed_document")
    op.drop_table("source_snapshot")
    op.drop_table("source_credential")
    op.drop_table("data_source")
    op.drop_index("uq_period_type_start_end", table_name="period")
    op.drop_table("period")
    op.drop_table("wallet")
    op.drop_table("employee")
    op.drop_table("counterparty_role")
    op.drop_index("uq_counterparty_inn_not_null", table_name="counterparty")
    op.drop_table("counterparty")
    op.drop_table("user_role")
    op.drop_table("location")
    op.drop_table("role")
    op.drop_table("user")
    op.drop_table("organization")
    _drop_enums()

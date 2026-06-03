"""add dds reconciliation cases

Revision ID: 0051_dds_reconciliation_cases
Revises: 0050_inv_item_signed_amount
Create Date: 2026-06-03
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0051_dds_reconciliation_cases"
down_revision = "0050_inv_item_signed_amount"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bank_operations",
        sa.Column("transfer_group_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_bank_operations_transfer_group_id_transfer_groups",
        "bank_operations",
        "transfer_groups",
        ["transfer_group_id"],
        ["id"],
    )

    op.create_table(
        "reconciliation_cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("bank_operation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("resolution_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
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
            ["bank_operation_id"],
            ["bank_operations.id"],
            name="fk_reconciliation_cases_bank_operation_id_bank_operations",
        ),
    )
    op.create_index(
        "ix_reconciliation_cases_status",
        "reconciliation_cases",
        ["status"],
    )
    op.create_index(
        "ix_reconciliation_cases_kind_status",
        "reconciliation_cases",
        ["kind", "status"],
    )
    op.create_index(
        "uq_reconciliation_cases_pending_operation_kind",
        "reconciliation_cases",
        ["kind", "bank_operation_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending' AND bank_operation_id IS NOT NULL"),
    )
    op.create_index(
        "uq_reconciliation_cases_pending_invalid_credentials",
        "reconciliation_cases",
        ["kind", "provider"],
        unique=True,
        postgresql_where=sa.text("status = 'pending' AND kind = 'invalid_credentials'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_reconciliation_cases_pending_invalid_credentials",
        table_name="reconciliation_cases",
    )
    op.drop_index(
        "uq_reconciliation_cases_pending_operation_kind",
        table_name="reconciliation_cases",
    )
    op.drop_index("ix_reconciliation_cases_kind_status", table_name="reconciliation_cases")
    op.drop_index("ix_reconciliation_cases_status", table_name="reconciliation_cases")
    op.drop_table("reconciliation_cases")
    op.drop_constraint(
        "fk_bank_operations_transfer_group_id_transfer_groups",
        "bank_operations",
        type_="foreignkey",
    )
    op.drop_column("bank_operations", "transfer_group_id")

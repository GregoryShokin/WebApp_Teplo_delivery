"""add deferred audit charges

Revision ID: 0054_deferred_audit_charge
Revises: 0053_inv_item_exclusions
Create Date: 2026-06-04
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0054_deferred_audit_charge"
down_revision = "0053_inv_item_exclusions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deferred_audit_charge",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("source_audit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("total_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("splits_count", sa.Integer(), nullable=False),
        sa.Column("splits_remaining", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "applied_run_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status in ('pending','partially_applied','applied','cancelled')",
            name="ck_deferred_audit_charge_status",
        ),
        sa.CheckConstraint(
            "splits_count > 0 AND splits_remaining >= 0 AND splits_remaining <= splits_count",
            name="ck_deferred_audit_charge_splits",
        ),
        sa.CheckConstraint(
            "total_amount > 0",
            name="ck_deferred_audit_charge_amount_positive",
        ),
        sa.ForeignKeyConstraint(
            ["source_audit_id"],
            ["inventory_audit.id"],
            name="fk_deferred_audit_charge_source_audit_id_inventory_audit",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_item_id"],
            ["inventory_audit_item.id"],
            name="fk_deferred_audit_charge_source_item_id_inventory_audit_item",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
            name="fk_deferred_audit_charge_employee_id_employee",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name="fk_deferred_audit_charge_created_by_user_id_user",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_deferred_audit_charge_employee_status",
        "deferred_audit_charge",
        ["employee_id", "status"],
    )
    op.create_index(
        "ix_deferred_audit_charge_audit",
        "deferred_audit_charge",
        ["source_audit_id"],
    )

    op.create_table(
        "deferred_audit_charge_split",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("charge_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("split_index", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("adjustment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["charge_id"],
            ["deferred_audit_charge.id"],
            name="fk_deferred_audit_charge_split_charge_id_deferred_audit_charge",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["payroll_run.id"],
            name="fk_deferred_audit_charge_split_run_id_payroll_run",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["adjustment_id"],
            ["payroll_adjustment.id"],
            name="fk_deferred_audit_charge_split_adjustment_id_payroll_adjustment",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "charge_id",
            "split_index",
            name="uq_deferred_charge_split_index",
        ),
    )
    op.create_index(
        "ix_deferred_charge_split_charge",
        "deferred_audit_charge_split",
        ["charge_id"],
    )
    op.create_index(
        "ix_deferred_charge_split_run",
        "deferred_audit_charge_split",
        ["run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_deferred_charge_split_run", table_name="deferred_audit_charge_split")
    op.drop_index("ix_deferred_charge_split_charge", table_name="deferred_audit_charge_split")
    op.drop_table("deferred_audit_charge_split")
    op.drop_index("ix_deferred_audit_charge_audit", table_name="deferred_audit_charge")
    op.drop_index(
        "ix_deferred_audit_charge_employee_status",
        table_name="deferred_audit_charge",
    )
    op.drop_table("deferred_audit_charge")

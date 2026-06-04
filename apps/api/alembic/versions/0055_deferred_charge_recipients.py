"""rework deferred audit charges with recipients

Revision ID: 0055_deferred_charge_recipients
Revises: 0054_deferred_audit_charge
Create Date: 2026-06-04
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0055_deferred_charge_recipients"
down_revision = "0054_deferred_audit_charge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("deferred_audit_charge_split")
    op.drop_table("deferred_audit_charge")

    op.create_table(
        "deferred_audit_charge",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("source_audit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("allocation_group", sa.String(length=16), nullable=False),
        sa.Column("total_penalty_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("splits_count", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
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
            "allocation_group in ('chefs','admins','common')",
            name="ck_deferred_charge_allocation_group",
        ),
        sa.CheckConstraint(
            "status in ('pending','partially_applied','applied','cancelled')",
            name="ck_deferred_charge_status",
        ),
        sa.CheckConstraint(
            "splits_count > 0",
            name="ck_deferred_charge_splits_positive",
        ),
        sa.CheckConstraint(
            "total_penalty_amount > 0",
            name="ck_deferred_charge_total_positive",
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
            ["created_by_user_id"],
            ["user.id"],
            name="fk_deferred_audit_charge_created_by_user_id_user",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_deferred_charge_audit",
        "deferred_audit_charge",
        ["source_audit_id"],
    )

    op.create_table(
        "deferred_audit_charge_recipient",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("charge_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("per_split_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("splits_remaining", sa.Integer(), nullable=False),
        sa.Column("collapsed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("collapse_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("collapse_adjustment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "per_split_amount >= 0",
            name="ck_deferred_charge_recipient_amount_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["charge_id"],
            ["deferred_audit_charge.id"],
            name="fk_dacr_charge",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
            name="fk_dacr_employee",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["collapse_run_id"],
            ["payroll_run.id"],
            name="fk_dacr_collapse_run",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["collapse_adjustment_id"],
            ["payroll_adjustment.id"],
            name="fk_dacr_collapse_adjustment",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "charge_id",
            "employee_id",
            name="uq_deferred_charge_recipient",
        ),
    )
    op.create_index(
        "ix_deferred_charge_recipient_employee",
        "deferred_audit_charge_recipient",
        ["employee_id"],
    )

    op.create_table(
        "deferred_audit_charge_split",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("recipient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("split_index", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("adjustment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["recipient_id"],
            ["deferred_audit_charge_recipient.id"],
            name="fk_dacs_recipient",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["payroll_run.id"],
            name="fk_dacs_run",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["adjustment_id"],
            ["payroll_adjustment.id"],
            name="fk_dacs_adjustment",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "recipient_id",
            "split_index",
            name="uq_deferred_charge_split_recipient_index",
        ),
    )
    op.create_index(
        "ix_deferred_charge_split_recipient",
        "deferred_audit_charge_split",
        ["recipient_id"],
    )
    op.create_index(
        "ix_deferred_charge_split_run",
        "deferred_audit_charge_split",
        ["run_id"],
    )


def downgrade() -> None:
    raise NotImplementedError("Deferred audit charge recipient migration is dev-only")

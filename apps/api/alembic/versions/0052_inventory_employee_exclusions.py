"""add inventory audit employee exclusions

Revision ID: 0052_inv_employee_exclusions
Revises: 0051_dds_reconciliation_cases
Create Date: 2026-06-03
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0052_inv_employee_exclusions"
down_revision = "0051_dds_reconciliation_cases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inventory_audit_employee_exclusion",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
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
            ["audit_id"],
            ["inventory_audit.id"],
            name="fk_inventory_audit_employee_exclusion_audit_id_inventory_audit",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
            name="fk_inventory_audit_employee_exclusion_employee_id_employee",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name="fk_inventory_audit_employee_exclusion_created_by_user_id_user",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "audit_id",
            "employee_id",
            name="uq_inventory_audit_employee_exclusion",
        ),
    )
    op.create_index(
        "ix_inventory_audit_employee_exclusion_audit",
        "inventory_audit_employee_exclusion",
        ["audit_id"],
    )
    op.create_index(
        "ix_inventory_audit_employee_exclusion_employee",
        "inventory_audit_employee_exclusion",
        ["employee_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inventory_audit_employee_exclusion_employee",
        table_name="inventory_audit_employee_exclusion",
    )
    op.drop_index(
        "ix_inventory_audit_employee_exclusion_audit",
        table_name="inventory_audit_employee_exclusion",
    )
    op.drop_table("inventory_audit_employee_exclusion")

"""add payroll payment marks

Revision ID: 0073_payroll_payment
Revises: 0072_payroll_run_event
Create Date: 2026-06-07
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0073_payroll_payment"
down_revision = "0072_payroll_run_event"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payroll_payment",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("paid_at", sa.Date(), nullable=False),
        sa.Column("method", sa.String(length=32), nullable=False),
        sa.Column("paid_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("amount >= 0", name="ck_payroll_payment_amount_non_negative"),
        sa.CheckConstraint(
            "method in ('business_card', 'cash', 'transfer', 'other')",
            name="ck_payroll_payment_method",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
            name="fk_payroll_payment_employee_id_employee",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["paid_by_user_id"],
            ["user.id"],
            name="fk_payroll_payment_paid_by_user_id_user",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["payroll_run.id"],
            name="fk_payroll_payment_run_id_payroll_run",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("run_id", "employee_id", name="uq_payroll_payment_run_employee"),
    )
    op.create_index("ix_payroll_payment_run_id", "payroll_payment", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_payroll_payment_run_id", table_name="payroll_payment")
    op.drop_table("payroll_payment")

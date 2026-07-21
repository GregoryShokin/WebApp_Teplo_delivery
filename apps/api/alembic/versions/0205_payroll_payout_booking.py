"""Связать выплату сотруднику с ДДС-проводкой для безопасного отката.

Revision ID: 0205_payroll_payout_booking
Revises: 0204_alloc_split_line
Create Date: 2026-07-21
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0205_payroll_payout_booking"
down_revision = "0204_alloc_split_line"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payroll_payout_booking",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("cashflow_transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reversal_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount > 0", name="ck_payroll_payout_booking_amount_positive"),
        sa.ForeignKeyConstraint(["run_id"], ["payroll_run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["payment_id"], ["payroll_payment.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["cashflow_transaction_id"], ["cashflow_transactions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reversal_transaction_id"], ["cashflow_transactions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_payroll_payout_booking_payment", "payroll_payout_booking", ["payment_id"])
    op.create_index(
        "ix_payroll_payout_booking_employee",
        "payroll_payout_booking",
        ["run_id", "employee_id"],
    )
    op.create_index(
        "ix_payroll_payout_booking_transaction",
        "payroll_payout_booking",
        ["cashflow_transaction_id"],
    )


def downgrade() -> None:
    op.drop_table("payroll_payout_booking")

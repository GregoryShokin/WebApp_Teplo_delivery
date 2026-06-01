"""add historical deposit initial balance

Revision ID: 0026_deposit_initial_balance
Revises: 0026_deposit_dismissal_types
Create Date: 2026-05-31
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0026_deposit_initial_balance"
down_revision = "0026_deposit_dismissal_types"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deposit_account",
        sa.Column(
            "initial_balance",
            sa.Numeric(14, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.execute(
        """
        UPDATE deposit_account a
        SET initial_balance = a.balance
        WHERE a.balance > 0
          AND NOT EXISTS (
            SELECT 1
            FROM deposit_transaction dt
            JOIN payroll_run pr ON pr.id = dt.run_id
            WHERE dt.employee_id = a.employee_id
              AND pr.status = 'finalized'
          )
        """
    )
    op.create_check_constraint(
        op.f("ck_deposit_account_initial_balance_non_negative"),
        "deposit_account",
        "initial_balance >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_deposit_account_initial_balance_non_negative"),
        "deposit_account",
        type_="check",
    )
    op.drop_column("deposit_account", "initial_balance")

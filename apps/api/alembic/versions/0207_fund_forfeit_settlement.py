"""Track when forfeited fund accounts leave the operational roster.

Revision ID: 0207_fund_forfeit_settlement
Revises: 0206_invoice_operational_scope
Create Date: 2026-07-22
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0207_fund_forfeit_settlement"
down_revision = "0206_invoice_operational_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accumulation_fund_account",
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Earlier payout runs did not mark forfeited accounts as closed. If the same
    # fund year already has a real payout transaction, close its forfeitures at
    # that payout time while preserving their financial history.
    op.execute(
        """
        UPDATE accumulation_fund_account AS account
        SET settled_at = payout.paid_at
        FROM (
            SELECT year, MIN(created_at) AS paid_at
            FROM accumulation_fund_transaction
            WHERE transaction_type = 'payout'
            GROUP BY year
        ) AS payout
        WHERE account.year = payout.year
          AND account.status = 'forfeited'
          AND account.settled_at IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("accumulation_fund_account", "settled_at")

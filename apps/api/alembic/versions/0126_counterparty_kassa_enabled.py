"""counterparty kassa_enabled flag

Adds ``kassa_enabled`` to ``counterparty_payable_profile`` — marks a supplier as
available in the invoice-creation dropdown of the Касса module only. Does not
affect the Warehouse picker or DDS classification. Defaults to ``false`` for all
existing suppliers (the Касса list starts empty and is curated manually).

Revision ID: 0126_counterparty_kassa_enabled
Revises: 0125_payroll_payout_cash_wallet
Create Date: 2026-06-19
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0126_counterparty_kassa_enabled"
down_revision = "0125_payroll_payout_cash_wallet"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "counterparty_payable_profile",
        sa.Column(
            "kassa_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("counterparty_payable_profile", "kassa_enabled")

"""Prepayment-model flag: bank debits to this supplier top up receivable automatically.

Owner case (2026-07-16): Mango Telecom is paid by card auto-debit — the money fact
arrives via the bank feed (DDS journal), not via an invoice. With this flag the
classifier turns every outgoing bank transaction to the supplier into an open
supplier_prepayment (linked to that transaction, no new money movement), and the
closing УПД from SBIS settles it — so the supplier balance is always current.

Revision ID: 0195_profile_bank_prepayment
Revises: 0194_counterparty_origin
Create Date: 2026-07-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0195_profile_bank_prepayment"
down_revision = "0194_counterparty_origin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "counterparty_payable_profile",
        sa.Column(
            "bank_payments_create_prepayment",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("counterparty_payable_profile", "bank_payments_create_prepayment")

"""payroll run payout cash wallet

Adds ``payout_cash_wallet_id`` to ``payroll_run`` — the cash wallet (Сейф / Торговая
касса Черникова) used for the cash portion of an admin payroll payout, so the cash-side
DDS transactions are booked against the right wallet.

Revision ID: 0125_payroll_payout_cash_wallet
Revises: 0124_courier_senior_target
Create Date: 2026-06-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0125_payroll_payout_cash_wallet"
down_revision = "0124_courier_senior_target"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payroll_run",
        sa.Column("payout_cash_wallet_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_payroll_run_payout_cash_wallet",
        "payroll_run",
        "wallet",
        ["payout_cash_wallet_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_payroll_run_payout_cash_wallet", "payroll_run", type_="foreignkey"
    )
    op.drop_column("payroll_run", "payout_cash_wallet_id")

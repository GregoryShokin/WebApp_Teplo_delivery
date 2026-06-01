"""allow fund initial balance

Revision ID: 0030_fund_initial_balance
Revises: 0029_fund_transactions
Create Date: 2026-05-31
"""

from __future__ import annotations

from alembic import op

revision = "0030_fund_initial_balance"
down_revision = "0029_fund_transactions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_accumulation_fund_transaction_type",
        "accumulation_fund_transaction",
        type_="check",
    )
    op.create_check_constraint(
        "ck_accumulation_fund_transaction_type",
        "accumulation_fund_transaction",
        "transaction_type IN ('accrual', 'payout', 'forfeit', 'initial_balance')",
    )
    op.drop_constraint(
        "ck_accumulation_fund_transaction_amount_positive",
        "accumulation_fund_transaction",
        type_="check",
    )
    op.create_check_constraint(
        "ck_accumulation_fund_transaction_amount_positive",
        "accumulation_fund_transaction",
        "(transaction_type = 'initial_balance' AND amount >= 0) "
        "OR (transaction_type <> 'initial_balance' AND amount > 0)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_accumulation_fund_transaction_amount_positive",
        "accumulation_fund_transaction",
        type_="check",
    )
    op.create_check_constraint(
        "ck_accumulation_fund_transaction_amount_positive",
        "accumulation_fund_transaction",
        "amount > 0",
    )
    op.drop_constraint(
        "ck_accumulation_fund_transaction_type",
        "accumulation_fund_transaction",
        type_="check",
    )
    op.create_check_constraint(
        "ck_accumulation_fund_transaction_type",
        "accumulation_fund_transaction",
        "transaction_type IN ('accrual', 'payout', 'forfeit')",
    )

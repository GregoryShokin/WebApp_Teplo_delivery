"""allow dismissal deposit transaction types

Revision ID: 0026_deposit_dismissal_types
Revises: 0025_deposit_per_employee_config
Create Date: 2026-05-31
"""

from __future__ import annotations

from alembic import op

revision = "0026_deposit_dismissal_types"
down_revision = "0025_deposit_per_employee_config"
branch_labels = None
depends_on = None

OLD_TYPES = "'accrual', 'payout', 'write_off'"
NEW_TYPES = f"{OLD_TYPES}, 'dismissal_payout', 'dismissal_writeoff'"


def upgrade() -> None:
    op.drop_constraint("ck_deposit_transaction_type", "deposit_transaction", type_="check")
    op.create_check_constraint(
        "ck_deposit_transaction_type",
        "deposit_transaction",
        f"transaction_type in ({NEW_TYPES})",
    )


def downgrade() -> None:
    op.execute(
        "update deposit_transaction set transaction_type = 'payout' "
        "where transaction_type = 'dismissal_payout'"
    )
    op.execute(
        "update deposit_transaction set transaction_type = 'write_off' "
        "where transaction_type = 'dismissal_writeoff'"
    )
    op.drop_constraint("ck_deposit_transaction_type", "deposit_transaction", type_="check")
    op.create_check_constraint(
        "ck_deposit_transaction_type",
        "deposit_transaction",
        f"transaction_type in ({OLD_TYPES})",
    )

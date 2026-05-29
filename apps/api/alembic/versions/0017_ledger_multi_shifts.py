"""allow multiple daily shift ledger entries

Revision ID: 0017_ledger_multi_shifts
Revises: 0016_employee_fire_reason
Create Date: 2026-05-29
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0017_ledger_multi_shifts"
down_revision = "0016_employee_fire_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_shift_ledger_entry_work_date_employee",
        "shift_ledger_entry",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_shift_ledger_entry_work_date_employee_opened",
        "shift_ledger_entry",
        ["work_date", "employee_id", "opened_at"],
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            delete from shift_ledger_entry duplicate
             using shift_ledger_entry kept
             where duplicate.work_date = kept.work_date
               and duplicate.employee_id = kept.employee_id
               and (
                    duplicate.opened_at > kept.opened_at
                    or (
                        duplicate.opened_at = kept.opened_at
                        and duplicate.id::text > kept.id::text
                    )
               )
            """
        )
    )
    op.drop_constraint(
        "uq_shift_ledger_entry_work_date_employee_opened",
        "shift_ledger_entry",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_shift_ledger_entry_work_date_employee",
        "shift_ledger_entry",
        ["work_date", "employee_id"],
    )

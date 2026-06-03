"""fix inventory audit penalty work dates

Revision ID: 0043_fix_audit_penalty_work_date
Revises: 0042_remap_inv_items_positions
Create Date: 2026-06-02
"""

from __future__ import annotations

from alembic import op

revision = "0043_fix_audit_penalty_work_date"
down_revision = "0042_remap_inv_items_positions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE payroll_adjustment pa
        SET work_date = pa.work_date + INTERVAL '1 day'
        WHERE EXISTS (
            SELECT 1
            FROM payroll_adjustment_category cat
            WHERE cat.id = pa.category_id
              AND cat.code = 'inventory_shortage'
        )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE payroll_adjustment pa
        SET work_date = pa.work_date - INTERVAL '1 day'
        WHERE EXISTS (
            SELECT 1
            FROM payroll_adjustment_category cat
            WHERE cat.id = pa.category_id
              AND cat.code = 'inventory_shortage'
        )
        """
    )

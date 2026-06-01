"""consolidate hot section station

Revision ID: 0038_consolidate_hot_section
Revises: 0037_shift_allowance_override
Create Date: 2026-06-01
"""

from __future__ import annotations

from alembic import op

revision = "0038_consolidate_hot_section"
down_revision = "0037_shift_allowance_override"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE scheduled_shift
           SET station_code = 'Горячий цех'
         WHERE station_code = 'Шаурма'
        """
    )


def downgrade() -> None:
    # Intentionally irreversible: "Горячий цех" may contain both legacy and valid rows.
    pass

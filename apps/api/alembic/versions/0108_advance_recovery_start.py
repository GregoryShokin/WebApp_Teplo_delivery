"""salary advance: дата начала удержания (recovery_start_date)

Заём может начинать гаситься не с ближайшей ведомости, а с заданной даты:
пока `period.end_date < recovery_start_date`, заём в удержание не попадает.
NULL = старое поведение (гасится с ближайшей подходящей ведомости).

Revision ID: 0108_advance_recovery_start
Revises: 0107_warehouse_barter
Create Date: 2026-06-15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0108_advance_recovery_start"
down_revision = "0107_warehouse_barter"
branch_labels = None
depends_on = None

_TABLE = "salary_advance"
_COLUMN = "recovery_start_date"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)

"""dishwasher shift manual entry

Смены мойщиц проставляются управляющим вручную (без iiko). Посменная оплата:
пул ₽/мес ÷ календарные дни месяца = ставка за смену; считается в полумесячной
окладной ведомости.

Revision ID: 0086_dishwasher_shift
Revises: 0085_admin_payroll_excluded
Create Date: 2026-06-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0086_dishwasher_shift"
down_revision = "0085_admin_payroll_excluded"
branch_labels = None
depends_on = None

_TABLE = "dishwasher_shift"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("employee_id", UUID(as_uuid=True), nullable=False),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("employee_id", "work_date", name="uq_dishwasher_shift_employee_date"),
    )
    op.create_index("ix_dishwasher_shift_work_date", _TABLE, ["work_date"])


def downgrade() -> None:
    op.drop_index("ix_dishwasher_shift_work_date", table_name=_TABLE)
    op.drop_table(_TABLE)

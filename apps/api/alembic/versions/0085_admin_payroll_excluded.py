"""employee admin payroll exclusion flag

Тоггл «исключить из ведомости администрации». Нужен для сотрудников, у которых
оклад формально есть (например по должности), но платить его не нужно — собственник
в роли управляющего, системные/AI-аккаунты и т.п.

Revision ID: 0085_admin_payroll_excluded
Revises: 0084_payroll_adjustment_role
Create Date: 2026-06-10
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0085_admin_payroll_excluded"
down_revision = "0084_payroll_adjustment_role"
branch_labels = None
depends_on = None

_TABLE = "employee"
_COLUMN = "admin_payroll_excluded"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)

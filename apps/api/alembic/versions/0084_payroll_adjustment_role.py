"""payroll adjustment role discriminator

Премия/штраф теперь привязаны к роли сотрудника на момент начисления. Это разводит
деньги по правильным ведомостям: производственная (недельная) ведомость подхватывает
только производственные роли, админская (полумесячная) — только админские. Без этого
штраф сотруднику с двумя ролями (например управляющий, вышедший поваром-субститутом)
попал бы в обе ведомости и удвоился.

Backfill: роль существующих записей = должность сотрудника на дату начисления
(employee_position_assignment), иначе 'Повар' как безопасный дефолт — все текущие
записи относятся к производственному персоналу.

Revision ID: 0084_payroll_adjustment_role
Revises: 0083_payroll_rate_admin_oklad
Create Date: 2026-06-10
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0084_payroll_adjustment_role"
down_revision = "0083_payroll_rate_admin_oklad"
branch_labels = None
depends_on = None

_TABLE = "payroll_adjustment"
_COLUMN = "role"
_INDEX = "ix_payroll_adjustment_role_date"

_BACKFILL = """
    UPDATE payroll_adjustment AS pa
    SET role = COALESCE((
        SELECT epa.position
        FROM employee_position_assignment AS epa
        WHERE epa.employee_id = pa.employee_id
          AND epa.effective_from <= pa.work_date
          AND (epa.effective_to IS NULL OR epa.effective_to >= pa.work_date)
        ORDER BY epa.effective_from DESC
        LIMIT 1
    ), 'Повар')
    WHERE pa.role IS NULL
"""


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(160), nullable=True))
    op.execute(_BACKFILL)
    op.alter_column(_TABLE, _COLUMN, nullable=False, server_default="Повар")
    op.create_index(_INDEX, _TABLE, [_COLUMN, "work_date"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, _COLUMN)

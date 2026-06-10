"""payroll rate admin oklad: per-employee override + admin category

Оклады администрации хранятся в `payroll_rate` (rate_type='monthly') как переиспользование
существующего версионируемого реестра ставок:
  - строка-дефолт на должность (employee_id IS NULL);
  - переопределение на конкретного сотрудника (employee_id указан) — имеет приоритет.

Для этого:
  - добавляется nullable `employee_id` с FK на employee;
  - натуральный уникальный ключ расщепляется на два частичных уникальных индекса
    (дефолты по должности и переопределения по сотруднику не конфликтуют);
  - категория `admin` добавляется в допустимые значения (оклад не привязан к
    производственным категориям повар/кассир).

Revision ID: 0083_payroll_rate_admin_oklad
Revises: 0082_payroll_period_half_month
Create Date: 2026-06-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0083_payroll_rate_admin_oklad"
down_revision = "0082_payroll_period_half_month"
branch_labels = None
depends_on = None

_TABLE = "payroll_rate"
_OLD_UNIQUE = "uq_payroll_rate_natural_effective_from"
_UNIQUE_DEFAULT = "uq_payroll_rate_natural_default"
_UNIQUE_EMPLOYEE = "uq_payroll_rate_natural_employee"
_CATEGORY_CHECK = "ck_payroll_rate_category_value"
_CATEGORY_VALUES_OLD = (
    "'category_1', 'category_2', 'category_3', 'category_4', 'intern', 'freelancer'"
)
_CATEGORY_VALUES_NEW = (
    "'category_1', 'category_2', 'category_3', 'category_4', 'intern', 'freelancer', 'admin'"
)


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("employee_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_payroll_rate_employee",
        _TABLE,
        "employee",
        ["employee_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_constraint(_OLD_UNIQUE, _TABLE, type_="unique")
    op.create_index(
        _UNIQUE_DEFAULT,
        _TABLE,
        ["position_group", "category", "station", "rate_type", "effective_from"],
        unique=True,
        postgresql_where=sa.text("employee_id IS NULL"),
    )
    op.create_index(
        _UNIQUE_EMPLOYEE,
        _TABLE,
        ["employee_id", "position_group", "category", "station", "rate_type", "effective_from"],
        unique=True,
        postgresql_where=sa.text("employee_id IS NOT NULL"),
    )

    op.drop_constraint(_CATEGORY_CHECK, _TABLE, type_="check")
    op.create_check_constraint(
        _CATEGORY_CHECK,
        _TABLE,
        f"category in ({_CATEGORY_VALUES_NEW})",
    )


def downgrade() -> None:
    op.drop_constraint(_CATEGORY_CHECK, _TABLE, type_="check")
    op.create_check_constraint(
        _CATEGORY_CHECK,
        _TABLE,
        f"category in ({_CATEGORY_VALUES_OLD})",
    )

    op.drop_index(_UNIQUE_EMPLOYEE, table_name=_TABLE)
    op.drop_index(_UNIQUE_DEFAULT, table_name=_TABLE)
    op.create_unique_constraint(
        _OLD_UNIQUE,
        _TABLE,
        ["position_group", "category", "station", "rate_type", "effective_from"],
    )

    op.drop_constraint("fk_payroll_rate_employee", _TABLE, type_="foreignkey")
    op.drop_column(_TABLE, "employee_id")

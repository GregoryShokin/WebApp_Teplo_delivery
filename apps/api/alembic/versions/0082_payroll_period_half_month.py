"""payroll period half-month type

Расчёт ЗП админ-персонала ведётся полумесячными ведомостями (1–15 и 16–конец
месяца) с выплатами 15-го и 1-го числа. Это требует разрешить второй тип периода
`half_month` рядом с производственным недельным `week`.

Revision ID: 0082_payroll_period_half_month
Revises: 0081_payroll_run_payout_cash
Create Date: 2026-06-10
"""

from __future__ import annotations

from alembic import op

revision = "0082_payroll_period_half_month"
down_revision = "0081_payroll_run_payout_cash"
branch_labels = None
depends_on = None

_TABLE = "payroll_period"
_OLD_CHECK = "ck_payroll_period_type_week"
_NEW_CHECK = "ck_payroll_period_type"


def upgrade() -> None:
    op.drop_constraint(_OLD_CHECK, _TABLE, type_="check")
    op.create_check_constraint(
        _NEW_CHECK,
        _TABLE,
        "period_type in ('week', 'half_month')",
    )


def downgrade() -> None:
    op.drop_constraint(_NEW_CHECK, _TABLE, type_="check")
    op.create_check_constraint(
        _OLD_CHECK,
        _TABLE,
        "period_type = 'week'",
    )

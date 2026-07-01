"""Авансы/займы через зарплатную ведомость: payout_method='payroll'.

Выдача через ведомость (сумма добавляется к ближайшей выплате, отдельного оттока нет)
использует `payout_method='payroll'` — расширяем CHECK-ограничение.

Revision ID: 0150_advance_via_payroll
Revises: 0149_revision_item_adjustment
Create Date: 2026-07-01
"""

from __future__ import annotations

from alembic import op

revision = "0150_advance_via_payroll"
down_revision = "0149_revision_item_adjustment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_salary_advance_payout_method", "salary_advance", type_="check")
    op.create_check_constraint(
        "ck_salary_advance_payout_method",
        "salary_advance",
        "payout_method is null or payout_method in "
        "('business_card', 'cash', 'transfer', 'other', 'payroll')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_salary_advance_payout_method", "salary_advance", type_="check")
    op.create_check_constraint(
        "ck_salary_advance_payout_method",
        "salary_advance",
        "payout_method is null or payout_method in "
        "('business_card', 'cash', 'transfer', 'other')",
    )

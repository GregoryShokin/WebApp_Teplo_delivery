"""Долг по штрафу в строке ведомости.

Решение владельца 31.10.2026: долг за неделю переходит в следующую ведомость и удерживается,
пока не погасится. До этой миграции излишек терялся: удержания больше начисленного давали 0 к
выплате, а разница нигде не оставалась. Три суммы стоят на одной строке-носителе человека,
равенство держится по человеку за неделю:
``сумма (начислено − НДФЛ − deduction) − penalty_debt_in + penalty_debt_out
+ penalty_debt_written_off = сумма total_payable до авансов, зачётов и округления``.

Revision ID: 0280_payroll_line_penalty_debt
Revises: 0279_correction_act_external_id
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0280_payroll_line_penalty_debt"
down_revision = "0279_correction_act_external_id"
branch_labels = None
depends_on = None

COLUMNS = ("penalty_debt_in", "penalty_debt_out", "penalty_debt_written_off")


def upgrade() -> None:
    for name in COLUMNS:
        op.add_column(
            "payroll_line",
            sa.Column(name, sa.Numeric(14, 2), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    for name in reversed(COLUMNS):
        op.drop_column("payroll_line", name)

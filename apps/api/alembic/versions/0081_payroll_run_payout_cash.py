"""payroll run-level total cash payout

Разбиение выплаты на наличную и расчётно-счётную части переносится с уровня
отдельных сотрудников на уровень всей ведомости. Новая колонка
`payroll_run.payout_cash_total` хранит общую наличную сумму, заданную владельцем;
РС-часть банковского черновика = ФОТ ведомости − эта сумма. Колонка переживает
ре-финализацию (в отличие от JSONB `summary`, который перезаписывается пересчётом).

Revision ID: 0081_payroll_run_payout_cash
Revises: 0080_courier_actor_user
Create Date: 2026-06-09
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0081_payroll_run_payout_cash"
down_revision = "0080_courier_actor_user"
branch_labels = None
depends_on = None

_TABLE = "payroll_run"
_COLUMN = "payout_cash_total"
_CHECK = "ck_payroll_run_payout_cash_nonneg"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.Numeric(14, 2),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(_CHECK, _TABLE, f"{_COLUMN} >= 0")


def downgrade() -> None:
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.drop_column(_TABLE, _COLUMN)

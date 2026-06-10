"""payroll_line.advance_recovered

Отдельная колонка под удержание аванса/займа в ведомости, чтобы возврат был явной
строкой и не смешивался со штрафами/депозитом в `deduction`. Заполняется сеймом
возврата при расчёте; `total_payable` уже уменьшен на эту сумму.

Revision ID: 0089_line_advance_recovered
Revises: 0088_salary_advance
Create Date: 2026-06-10
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0089_line_advance_recovered"
down_revision = "0088_salary_advance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payroll_line",
        sa.Column(
            "advance_recovered",
            sa.Numeric(14, 2),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("payroll_line", "advance_recovered")

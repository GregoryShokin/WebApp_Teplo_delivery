"""Вычет НДФЛ — из вопросов «сколько детей» и «единственный родитель», не ручной суммой.

Решение владельца 26.07.2026 (поздний вечер): ручная сумма вычета — источник ошибок.
Храним входные данные (число детей, единственный родитель), вычет считается сам по
ст. 218 НК (1-й ребёнок 1 400, 2-й 2 800, 3-й+ 6 000, суммируются; единственному
родителю — вдвое) с лимитом дохода 450 000 нарастающим итогом.

Колонка ``official_ndfl_deduction`` уходит: на стенде значение 1 400 соответствует
одному ребёнку — данные переносим в ``official_children_count``.

Revision ID: 0214_official_children
Revises: 0213_official_payroll
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0214_official_children"
down_revision = "0213_official_payroll"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employee",
        sa.Column("official_children_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "employee",
        sa.Column(
            "official_single_parent", sa.Boolean(), nullable=False, server_default="false"
        ),
    )
    op.create_check_constraint(
        "ck_employee_official_children_non_negative",
        "employee",
        "official_children_count >= 0",
    )
    # Перенос данных: 1 400/мес — ровно один ребёнок (других значений в контуре не было).
    op.execute(
        "update employee set official_children_count = 1 "
        "where official_ndfl_deduction = 1400"
    )
    op.drop_column("employee", "official_ndfl_deduction")


def downgrade() -> None:
    op.add_column(
        "employee",
        sa.Column(
            "official_ndfl_deduction",
            sa.Numeric(precision=12, scale=2),
            nullable=False,
            server_default="0",
        ),
    )
    op.execute(
        "update employee set official_ndfl_deduction = 1400 "
        "where official_children_count = 1"
    )
    op.drop_constraint(
        "ck_employee_official_children_non_negative", "employee", type_="check"
    )
    op.drop_column("employee", "official_single_parent")
    op.drop_column("employee", "official_children_count")

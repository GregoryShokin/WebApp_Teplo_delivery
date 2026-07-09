"""Аванс/заём из разбора операции ДДС: ссылка на исходную банковскую операцию.

Разбор исходящей операции на статью аванса/займа сотрудника заводит ``SalaryAdvance`` (деньги
уже ушли банком, второй проводки нет). Колонка ``source_operation_id`` связывает аванс с
операцией — для идемпотентности повторного разбора (снять прежний непогашенный аванс операции).

Revision ID: 0177_advance_source_operation
Revises: 0176_safe_allocation_employee
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0177_advance_source_operation"
down_revision = "0176_safe_allocation_employee"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("salary_advance", sa.Column("source_operation_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_salary_advance_source_operation_id_bank_operations",
        "salary_advance",
        "bank_operations",
        ["source_operation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_salary_advance_source_operation", "salary_advance", ["source_operation_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_salary_advance_source_operation", table_name="salary_advance")
    op.drop_constraint(
        "fk_salary_advance_source_operation_id_bank_operations",
        "salary_advance",
        type_="foreignkey",
    )
    op.drop_column("salary_advance", "source_operation_id")

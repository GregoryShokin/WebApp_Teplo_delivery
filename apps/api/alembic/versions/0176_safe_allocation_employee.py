"""Зарплатная целёвка Сейфа: сотрудник-получатель на резерве.

Разбор зарплатной операции «через Сейф» заводит резерв SafeAllocation под конкретного
сотрудника; при оплате резерва («Выплачено») создаётся EmployeePayout под него, и расчёт ЗП
учитывает «уже выплачено». Колонка ``employee_id`` — этот получатель (NULL у не-зарплатных
резервов: неофициальные поставщики, ручные целёвки).

Revision ID: 0176_safe_allocation_employee
Revises: 0175_dds_employee_payout_offset
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0176_safe_allocation_employee"
down_revision = "0175_dds_employee_payout_offset"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("safe_allocations", sa.Column("employee_id", sa.Uuid(), nullable=True))
    op.add_column(
        "safe_allocations", sa.Column("source_operation_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_safe_allocations_employee_id_employee",
        "safe_allocations",
        "employee",
        ["employee_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_safe_allocations_source_operation_id_bank_operations",
        "safe_allocations",
        "bank_operations",
        ["source_operation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_safe_allocations_employee_id", "safe_allocations", ["employee_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_safe_allocations_employee_id", table_name="safe_allocations")
    op.drop_constraint(
        "fk_safe_allocations_source_operation_id_bank_operations",
        "safe_allocations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_safe_allocations_employee_id_employee", "safe_allocations", type_="foreignkey"
    )
    op.drop_column("safe_allocations", "source_operation_id")
    op.drop_column("safe_allocations", "employee_id")

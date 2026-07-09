"""Учёт выплаты сотруднику из журнала ДДС в расчёте ЗП («уже выплачено»).

Механизм: при разборе банковской операции с зарплатной статьёй оператор указывает
сотрудника-получателя → заводится ``EmployeePayout`` (леджер «выплачено»), а расчёт
ведомости вычитает его из «к выдаче». Зеркало контура возврата авансов.

Добавляет:
- ``employee_payout.offset_amount`` — баланс потребления выплаты (зеркало
  ``salary_advance.recovered_amount``); ``outstanding = amount − offset_amount``.
- ``payroll_line.employee_payout_offset`` — сумма «уже выплачено банком», зачтённая
  в ведомости (зеркало ``advance_recovered``), для строки расчётника.
- таблицу ``employee_payout_offset`` — превью-факты зачёта per-run (зеркало
  ``salary_advance_recovery``): удаляются на ре-расчёте, двигают баланс на финализации.
- индекс периодного запроса ``employee_payout(kind, status, payout_date)``.

Revision ID: 0173_dds_employee_payout_offset
Revises: 0172_expense_draft_bank_provider
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0173_dds_employee_payout_offset"
down_revision = "0172_expense_draft_bank_provider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employee_payout",
        sa.Column(
            "offset_amount", sa.Numeric(14, 2), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "payroll_line",
        sa.Column(
            "employee_payout_offset", sa.Numeric(14, 2), nullable=False, server_default="0"
        ),
    )
    op.create_index(
        "ix_employee_payout_period",
        "employee_payout",
        ["kind", "status", "payout_date"],
    )
    op.create_table(
        "employee_payout_offset",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("employee_payout_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("amount > 0", name="ck_employee_payout_offset_amount_positive"),
        sa.ForeignKeyConstraint(
            ["employee_payout_id"],
            ["employee_payout.id"],
            name="fk_employee_payout_offset_payout",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["payroll_run.id"],
            name="fk_employee_payout_offset_run",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_employee_payout_offset"),
    )
    op.create_index(
        "ix_employee_payout_offset_payout", "employee_payout_offset", ["employee_payout_id"]
    )
    op.create_index("ix_employee_payout_offset_run", "employee_payout_offset", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_employee_payout_offset_run", table_name="employee_payout_offset")
    op.drop_index("ix_employee_payout_offset_payout", table_name="employee_payout_offset")
    op.drop_table("employee_payout_offset")
    op.drop_index("ix_employee_payout_period", table_name="employee_payout")
    op.drop_column("payroll_line", "employee_payout_offset")
    op.drop_column("employee_payout", "offset_amount")

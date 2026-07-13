"""Резерв-контейнер выплаты ЗП: привязка ``SafeAllocation`` к ведомости (``source_run_id``).

Контур «выплата ЗП из резервов, привязанных к ведомости». Финализация ведомости с
разбивкой нал/безнал заводит резервы-контейнеры: безналичный (``location='safe'``, после
транзита банк→Сейф) и наличный (``location='kassa'``). Резерв с ``employee_id IS NULL`` —
это ПУЛ на всю ведомость: он НЕ книжит cashflow сам, только ``amount_paid`` растёт как
счётчик потребления пула; реальные проводки и учёт «выплачено» идут через
``PayrollPayment`` (``mark_partial_payment`` → ``book_payout_expense_for_employees``).

``source_run_id`` — FK на ``payroll_run`` (SET NULL при удалении ведомости). Partial-unique
индекс ``(source_run_id, location)`` среди НЕотменённых гарантирует не более одного активного
резерва на (ведомость, локация) — один безналичный + один наличный пул, с возможностью
пересборки после дефинализации (отменённые исключены из уникума).

Revision ID: 0186_payroll_reserve_run_link
Revises: 0185_payroll_partial_payment
Create Date: 2026-07-12
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0186_payroll_reserve_run_link"
down_revision = "0185_payroll_partial_payment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "safe_allocations",
        sa.Column("source_run_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_safe_allocations_source_run_id_payroll_run",
        "safe_allocations",
        "payroll_run",
        ["source_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "uq_safe_allocations_source_run_location",
        "safe_allocations",
        ["source_run_id", "location"],
        unique=True,
        postgresql_where=sa.text(
            "source_run_id IS NOT NULL AND status IN ('reserved', 'partially_paid')"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_safe_allocations_source_run_location", table_name="safe_allocations")
    op.drop_constraint(
        "fk_safe_allocations_source_run_id_payroll_run",
        "safe_allocations",
        type_="foreignkey",
    )
    op.drop_column("safe_allocations", "source_run_id")

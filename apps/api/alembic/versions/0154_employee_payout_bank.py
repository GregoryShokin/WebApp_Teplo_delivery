"""Банковский контур выплат сотрудникам: поля черновика + Сейф-резерв + привязка к операции.

Track B2/B3. Банковская выплата (Сбер/Т-Банк) сотруднику: создаётся черновик Т-Банк по реквизитам
ИП (как выдача аванса), при подтверждении — транзит банк→Сейф + резерв (``SafeAllocation``), а
привязка к реальной банковской ОПЕРАЦИИ из выписки закрывает выплату. Наличная/сейфовая выплата
(Track A/B1) полями не пользуется.

Добавляем в ``employee_payout``:
* ``document_id`` / ``provider_ref`` / ``payload`` / ``last_error`` / ``synced_at`` — состояние
  платёжного черновика Т-Банк (зеркало ``salary_advance_bank_draft``);
* ``safe_allocation_id`` — резерв Сейфа, созданный при исполнении;
* ``bank_operation_id`` — привязанная банковская операция из выписки (ручное подтверждение).

Revision ID: 0154_employee_payout_bank
Revises: 0153_employee_payout
Create Date: 2026-07-02
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0154_employee_payout_bank"
down_revision = "0153_employee_payout"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employee_payout",
        sa.Column("document_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "employee_payout",
        sa.Column("provider_ref", sa.Text(), nullable=True),
    )
    op.add_column(
        "employee_payout",
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "employee_payout",
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "employee_payout",
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "employee_payout",
        sa.Column("safe_allocation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "employee_payout",
        sa.Column("bank_operation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_employee_payout_safe_allocation",
        "employee_payout",
        "safe_allocations",
        ["safe_allocation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_employee_payout_bank_operation",
        "employee_payout",
        "bank_operations",
        ["bank_operation_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_employee_payout_bank_operation", "employee_payout", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_employee_payout_safe_allocation", "employee_payout", type_="foreignkey"
    )
    op.drop_column("employee_payout", "bank_operation_id")
    op.drop_column("employee_payout", "safe_allocation_id")
    op.drop_column("employee_payout", "synced_at")
    op.drop_column("employee_payout", "last_error")
    op.drop_column("employee_payout", "payload")
    op.drop_column("employee_payout", "provider_ref")
    op.drop_column("employee_payout", "document_id")

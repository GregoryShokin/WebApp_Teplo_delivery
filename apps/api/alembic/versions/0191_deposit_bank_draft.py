"""Полный цикл выдачи депозита банк-каналом (черновик → активные платежи → оплата → резерв → выплата).

Раньше выдача депозита банк-каналом списывала депозит и книжила расход сразу, а черновик
уходил в банк без следа в БД. Теперь депозит живёт как аванс/ЗП: черновик виден в «Активных
платежах», оплата приходит вебхуком (Т-Банк)/поллингом (Сбер), тогда заводится транзит
р/с→Сейф и резерв Сейфа; депозит-счёт списывается только при фактической выдаче.

Добавляем таблицу ``deposit_bank_draft`` — зеркало ``salary_advance_bank_draft`` с двумя
получателями: производственник (``employee_id`` + ``deposit_transaction_id``, UUID) и курьер
(``courier_deposit_transaction_id``, Integer). Ссылка на транзакцию nullable — она возникает
при выдаче, а не при отправке черновика (списывать нечего, пока деньги не выданы).

Revision ID: 0191_deposit_bank_draft
Revises: 0189_payroll_draft_deleted
Create Date: 2026-07-16
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0191_deposit_bank_draft"
down_revision = "0189_payroll_draft_deleted"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deposit_bank_draft",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("recipient_kind", sa.String(16), nullable=False),
        sa.Column(
            "employee_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("employee.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "deposit_transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("deposit_transaction.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "courier_deposit_transaction_id",
            sa.BigInteger(),
            sa.ForeignKey("courier_deposit_transaction.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column(
            "bank_provider",
            sa.String(16),
            nullable=False,
            server_default="tbank",
        ),
        sa.Column("provider_ref", sa.Text(), nullable=True),
        sa.Column(
            "safe_allocation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("safe_allocations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status in ('created', 'updated', 'paid', 'disbursed', 'failed', 'deleted', 'cancelled')",
            name="ck_deposit_bank_draft_status",
        ),
        sa.CheckConstraint(
            "recipient_kind in ('production', 'courier')",
            name="ck_deposit_bank_draft_recipient_kind",
        ),
        sa.CheckConstraint(
            "(recipient_kind = 'production' AND employee_id IS NOT NULL "
            "AND courier_deposit_transaction_id IS NULL) "
            "OR (recipient_kind = 'courier' AND courier_deposit_transaction_id IS NOT NULL)",
            name="ck_deposit_bank_draft_recipient_ref",
        ),
    )
    op.create_index(
        "ix_deposit_bank_draft_status",
        "deposit_bank_draft",
        ["status"],
    )
    # Один активный черновик на производственника (гард от двойной отправки).
    op.create_index(
        "uq_deposit_bank_draft_active_employee",
        "deposit_bank_draft",
        ["employee_id"],
        unique=True,
        postgresql_where=sa.text(
            "status in ('created', 'updated', 'paid') AND employee_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_deposit_bank_draft_active_employee", "deposit_bank_draft")
    op.drop_index("ix_deposit_bank_draft_status", "deposit_bank_draft")
    op.drop_table("deposit_bank_draft")

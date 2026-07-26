"""Налоги: банк-черновик налоговой платёжки в очереди «Активные платежи».

Таблица ``tax_bank_draft`` делает налоговый платёж видимым и проверяемым до отправки в банк:
кнопка «Отправить в банк» на «Налогах» создаёт строку ``ready_to_send``, платёж появляется в
окне активных платежей, где владелец сверяет сумму/назначение (реквизиты ФНС фиксированы) и
уже оттуда отправляет в Т-Банк (``in_bank``). Факт списания приходит из выписки отдельно.

Revision ID: 0212_tax_bank_draft
Revises: 0211_tax_payroll_ledger
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0212_tax_bank_draft"
down_revision = "0211_tax_payroll_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tax_bank_draft",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tax_kind", sa.String(length=32), nullable=False),
        sa.Column("for_year", sa.Integer(), nullable=True),
        sa.Column("for_period", sa.String(length=16), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("kbk", sa.String(length=20), nullable=True),
        sa.Column("recipient_name", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="ready_to_send"),
        sa.Column("bank_provider", sa.String(length=16), nullable=False, server_default="tbank"),
        sa.Column("document_id", sa.String(length=64), nullable=True),
        sa.Column("provider_ref", sa.String(length=128), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tax_bank_draft"),
        sa.CheckConstraint(
            "status in ('ready_to_send', 'in_bank', 'paid', 'cancelled', 'failed')",
            name="ck_tax_bank_draft_status",
        ),
        sa.CheckConstraint("amount > 0", name="ck_tax_bank_draft_amount"),
    )
    op.create_index("ix_tax_bank_draft_status", "tax_bank_draft", ["status"])


def downgrade() -> None:
    op.drop_index("ix_tax_bank_draft_status", table_name="tax_bank_draft")
    op.drop_table("tax_bank_draft")

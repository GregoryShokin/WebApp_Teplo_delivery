"""Схема модуля «Касса»: дискриминатор чека + кассовые смены iiko.

Фаза 1 модуля «Касса» (самодостаточна, существующее не трогает):
1. Значение ``kassa_cheque`` в enum ``counterparty_invoice_source`` — чек хранится в
   общей таблице ``supplier_invoice`` как уже оплаченная картой накладная.
2. Таблица ``iiko_cash_shift`` — сводка закрытой кассовой смены iiko (витрина
   «Касса → Закрытие смены» + источник авто-проводки наличного контура в ДДС).
3. Таблица ``iiko_cash_shift_payout`` — разнос изъятий смены (``payOutsRecords``) по
   счетам-назначениям; ссылается на созданное движение ДДС.

``ADD VALUE`` нельзя выполнять в транзакции — вынесен в autocommit-блок (паттерн из
``0046_dds_core_schema``). ``DROP VALUE`` PostgreSQL не поддерживает, поэтому downgrade
значение enum не удаляет.

Revision ID: 0118_kassa_cheque_and_shifts
Revises: 0117_invoice_permissions
Create Date: 2026-06-17
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0118_kassa_cheque_and_shifts"
down_revision = "0117_invoice_permissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            sa.text("ALTER TYPE counterparty_invoice_source ADD VALUE IF NOT EXISTS 'kassa_cheque'")
        )

    op.create_table(
        "iiko_cash_shift",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("iiko_session_id", sa.String(length=128), nullable=False),
        sa.Column("session_number", sa.Integer(), nullable=True),
        sa.Column("point_of_sale_id", sa.String(length=128), nullable=True),
        sa.Column("manager_id", sa.String(length=128), nullable=True),
        sa.Column("open_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("session_status", sa.String(length=32), nullable=True),
        sa.Column("session_start_cash", sa.Numeric(18, 2), nullable=True),
        sa.Column("sales_cash", sa.Numeric(18, 2), nullable=True),
        sa.Column("sales_card", sa.Numeric(18, 2), nullable=True),
        sa.Column("pay_in", sa.Numeric(18, 2), nullable=True),
        sa.Column("pay_out", sa.Numeric(18, 2), nullable=True),
        sa.Column("cash_remain", sa.Numeric(18, 2), nullable=True),
        sa.Column("cash_diff", sa.Numeric(18, 2), nullable=True),
        sa.Column("posted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "synced_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_iiko_cash_shift_session", "iiko_cash_shift", ["iiko_session_id"], unique=True
    )
    op.create_index(
        "ix_iiko_cash_shift_close_date", "iiko_cash_shift", ["close_date"], unique=False
    )

    op.create_table(
        "iiko_cash_shift_payout",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("shift_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("iiko_payout_id", sa.String(length=128), nullable=True),
        sa.Column("account_id_iiko", sa.String(length=128), nullable=False),
        sa.Column("account_name", sa.String(length=255), nullable=True),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("cashflow_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["shift_id"], ["iiko_cash_shift.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["cashflow_transaction_id"], ["cashflow_transactions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_iiko_cash_shift_payout_shift", "iiko_cash_shift_payout", ["shift_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_iiko_cash_shift_payout_shift", table_name="iiko_cash_shift_payout")
    op.drop_table("iiko_cash_shift_payout")
    op.drop_index("ix_iiko_cash_shift_close_date", table_name="iiko_cash_shift")
    op.drop_index("uq_iiko_cash_shift_session", table_name="iiko_cash_shift")
    op.drop_table("iiko_cash_shift")
    # Значение enum 'kassa_cheque' не удаляем — PostgreSQL не поддерживает DROP VALUE.

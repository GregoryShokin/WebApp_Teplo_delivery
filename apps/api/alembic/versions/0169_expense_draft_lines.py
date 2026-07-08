"""Мультистрочный транш окна «Новый платёж»: строки expense-черновика + per-line целёвки.

Свободный вывод на Сейф теперь может нести НЕСКОЛЬКО статей одним банковским черновиком
(один транш на карту ИП), а разбивка помнится и при выплате с Сейфа разносится по статьям.

- Таблица ``expense_draft_line`` — строки транша (статья, сумма, назначение) одного
  ``counterparty_payment_draft``; сумма черновика = Σ строк.
- ``safe_allocations.source_draft_line_id`` — целёвка привязана к строке транша:
  paid-переход заводит ОДНУ целёвку на строку (идемпотентность по строке), поэтому на
  один черновик приходится несколько целёвок. Частичный уникум по ``source_draft_id``
  ослаблен до «одиночных» целёвок без строки (неофициальный поставщик via-safe), а
  строчные целёвки уникальны по ``source_draft_line_id``.

Revision ID: 0169_expense_draft_lines
Revises: 0168_sber_payout_channel
Create Date: 2026-07-08
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0169_expense_draft_lines"
down_revision = "0168_sber_payout_channel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "expense_draft_line",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("draft_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("article_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["draft_id"], ["counterparty_payment_draft.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["article_id"], ["dds_articles.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_expense_draft_line_draft", "expense_draft_line", ["draft_id"])

    op.add_column(
        "safe_allocations",
        sa.Column("source_draft_line_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_safe_alloc_source_draft_line",
        "safe_allocations",
        "expense_draft_line",
        ["source_draft_line_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Одиночный via-safe резерв (неофициальный поставщик) остаётся уникальным по черновику,
    # но строчные целёвки транша (несколько на черновик) под этот уникум не подпадают.
    op.drop_index("uq_safe_allocations_source_draft", table_name="safe_allocations")
    op.create_index(
        "uq_safe_allocations_source_draft",
        "safe_allocations",
        ["source_draft_id"],
        unique=True,
        postgresql_where=sa.text(
            "source_draft_id IS NOT NULL AND source_draft_line_id IS NULL"
        ),
    )
    op.create_index(
        "uq_safe_allocations_source_draft_line",
        "safe_allocations",
        ["source_draft_line_id"],
        unique=True,
        postgresql_where=sa.text("source_draft_line_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_safe_allocations_source_draft_line", table_name="safe_allocations")
    op.drop_index("uq_safe_allocations_source_draft", table_name="safe_allocations")
    op.create_index(
        "uq_safe_allocations_source_draft",
        "safe_allocations",
        ["source_draft_id"],
        unique=True,
        postgresql_where=sa.text("source_draft_id IS NOT NULL"),
    )
    op.drop_constraint(
        "fk_safe_alloc_source_draft_line", "safe_allocations", type_="foreignkey"
    )
    op.drop_column("safe_allocations", "source_draft_line_id")
    op.drop_index("ix_expense_draft_line_draft", table_name="expense_draft_line")
    op.drop_table("expense_draft_line")

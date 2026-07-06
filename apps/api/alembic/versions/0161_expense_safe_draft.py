"""Окно «Новый платёж»: свободный вывод на Сейф по любой расходной статье.

- ``counterparty_payment_draft.target_article_id`` / ``target_purpose`` — целевая
  статья и назначение via-safe черновика без получателя («просто трата»): paid-переход
  заводит транзит р/с→Сейф и целёвку ЭТОЙ статьи с ЭТИМ назначением (вместо жёстких
  «Оплата поставщикам» + purpose из накладных).
- ``counterparty_payment_draft.counterparty_id`` становится nullable: у черновика
  «просто траты» получателя-контрагента нет.

Revision ID: 0161_expense_safe_draft
Revises: 0160_kassa_target_stage3
Create Date: 2026-07-06
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0161_expense_safe_draft"
down_revision = "0160_kassa_target_stage3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "counterparty_payment_draft",
        sa.Column("target_article_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_cp_payment_draft_target_article",
        "counterparty_payment_draft",
        "dds_articles",
        ["target_article_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "counterparty_payment_draft",
        sa.Column("target_purpose", sa.Text(), nullable=True),
    )
    op.alter_column(
        "counterparty_payment_draft",
        "counterparty_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )


def downgrade() -> None:
    # Черновики «просто траты» без контрагента не переживают откат NOT NULL.
    op.execute("DELETE FROM counterparty_payment_draft WHERE counterparty_id IS NULL")
    op.alter_column(
        "counterparty_payment_draft",
        "counterparty_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.drop_column("counterparty_payment_draft", "target_purpose")
    op.drop_constraint(
        "fk_cp_payment_draft_target_article",
        "counterparty_payment_draft",
        type_="foreignkey",
    )
    op.drop_column("counterparty_payment_draft", "target_article_id")

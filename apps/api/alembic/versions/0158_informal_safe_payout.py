"""Оплата неофициальных поставщиков через вывод на карту ИП (Сейф).

- ``counterparty_payment_draft.pays_via_safe`` — черновик выписан на карту ИП
  (неофициальный поставщик): paid-переход заводит перевод р/с→Сейф и целевой
  резерв Сейфа вместо расхода «Оплата поставщикам».
- ``safe_allocations.source_draft_id`` — происхождение авто-резерва (черновик,
  по оплате которого он создан); уникально: один черновик — один авто-резерв.

Revision ID: 0158_informal_safe_payout
Revises: 0157_cheque_line_return
Create Date: 2026-07-05
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0158_informal_safe_payout"
down_revision = "0157_cheque_line_return"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "counterparty_payment_draft",
        sa.Column(
            "pays_via_safe",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "safe_allocations",
        sa.Column("source_draft_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_safe_allocations_source_draft",
        "safe_allocations",
        "counterparty_payment_draft",
        ["source_draft_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "uq_safe_allocations_source_draft",
        "safe_allocations",
        ["source_draft_id"],
        unique=True,
        postgresql_where=sa.text("source_draft_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_safe_allocations_source_draft", table_name="safe_allocations")
    op.drop_constraint(
        "fk_safe_allocations_source_draft", "safe_allocations", type_="foreignkey"
    )
    op.drop_column("safe_allocations", "source_draft_id")
    op.drop_column("counterparty_payment_draft", "pays_via_safe")

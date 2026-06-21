"""Резервы (аллокации) подотчётного счёта «Сейф» — фаза 2.

Таблица ``safe_allocations``: намерение потратить часть остатка Сейфа на
конкретного получателя/статью. Резерв не двигает баланс (деньги ещё на карте),
оплата (в т.ч. частичная) создаёт out-``CashflowTransaction`` с Сейфа; ``amount_paid``
хранит сумму оплат. См. модель ``app.models.dds.SafeAllocation``.

Revision ID: 0130_safe_allocations
Revises: 0129_kassa_pending_cheque
Create Date: 2026-06-21
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0130_safe_allocations"
down_revision = "0129_kassa_pending_cheque"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "safe_allocations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "wallet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("wallet.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("amount_paid", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column(
            "article_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dds_articles.id"),
            nullable=True,
        ),
        sa.Column(
            "counterparty_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("counterparty.id"),
            nullable=True,
        ),
        sa.Column("purpose", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="reserved"),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_safe_allocations_wallet_id", "safe_allocations", ["wallet_id"])
    op.create_index("ix_safe_allocations_status", "safe_allocations", ["status"])


def downgrade() -> None:
    op.drop_index("ix_safe_allocations_status", table_name="safe_allocations")
    op.drop_index("ix_safe_allocations_wallet_id", table_name="safe_allocations")
    op.drop_table("safe_allocations")

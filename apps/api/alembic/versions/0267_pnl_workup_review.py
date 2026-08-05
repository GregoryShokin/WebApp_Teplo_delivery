"""Очередь подтверждения проработки и след созданного акта списания.

ЗАМЫСЕЛ ВЛАДЕЛЬЦА 05.08.2026: накладная приходит — система помечает её «Требует проверки» и
спрашивает, проработка ли это. Если да, акт списания создаёт САМА, а не управляющий руками.
Так расход перестаёт задваиваться (он признаётся актом), а человек не делает лишней работы.

ПОЧЕМУ ТАБЛИЦА, А НЕ ФЛАГ У НАКЛАДНОЙ. Проработкой бывает одна позиция из десяти, а решение
живёт по товару и месяцу — так же, как ``pnl_product_monthly_decision``. Флаг на документе не
ответил бы, про какую строку речь.

ПОЧЕМУ ХРАНИМ ID СОЗДАННОГО АКТА. Cloud ``create`` не идемпотентен: каждый вызов плодит новый
документ в боевой iiko (проверено 05.08.2026 — акт 0458 создан и отменён). Гейт повтора может
быть только на нашей стороне, и это ``writeoff_document_id``.

Revision ID: 0267_pnl_workup_review
Revises: 0266_pnl_workup_memo
Create Date: 2026-08-05
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0267_pnl_workup_review"
down_revision = "0266_pnl_workup_memo"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pnl_workup_review",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("iiko_product_guid", sa.String(64), nullable=False),
        sa.Column("product_name", sa.String(255), nullable=True),
        sa.Column(
            "invoice_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("supplier_invoice.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("purchase_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("quantity", sa.Numeric(14, 3), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column(
            "decided_by_user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("writeoff_document_id", sa.String(64), nullable=True),
        sa.Column("writeoff_number", sa.String(64), nullable=True),
        sa.Column("writeoff_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status in ('pending', 'confirmed', 'rejected')",
            name="ck_pnl_workup_review_status",
        ),
    )
    op.create_index(
        "uq_pnl_workup_review_slot",
        "pnl_workup_review",
        ["period_month", "iiko_product_guid"],
        unique=True,
    )
    op.create_index("ix_pnl_workup_review_status", "pnl_workup_review", ["status"])


def downgrade() -> None:
    op.drop_index("ix_pnl_workup_review_status", table_name="pnl_workup_review")
    op.drop_index("uq_pnl_workup_review_slot", table_name="pnl_workup_review")
    op.drop_table("pnl_workup_review")

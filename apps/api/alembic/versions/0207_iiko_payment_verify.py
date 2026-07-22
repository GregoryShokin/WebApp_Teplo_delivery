"""iiko payment push: verification state

Ответ ``add_payment`` (201 + accountingTransactionId) НЕ доказывает проводку в учёте iiko —
22.07.2026 сверка показала 4 «тихие потери» из 55 успешных отправок. Держим состояние
пост-проверки на строке пуша: когда проводка подтверждена, сколько раз не нашли, сколько раз
переотправляли.

Revision ID: 0207_iiko_payment_verify
Revises: 0206_invoice_operational_scope
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0207_iiko_payment_verify"
down_revision = "0206_invoice_operational_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "iiko_invoice_payment_push",
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "iiko_invoice_payment_push",
        sa.Column("verify_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "iiko_invoice_payment_push",
        sa.Column("resend_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("iiko_invoice_payment_push", "resend_count")
    op.drop_column("iiko_invoice_payment_push", "verify_attempts")
    op.drop_column("iiko_invoice_payment_push", "verified_at")

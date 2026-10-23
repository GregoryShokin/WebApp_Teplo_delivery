"""Отметка «сторож последний раз видел оплату в iiko» у журнала отправок оплат.

``verified_at`` подтверждает проводку один раз, а снятую позже оплату никто не замечал:
13.10.2026 в iiko руками сняли оплаты чеков «Местного закупа», подтверждённые в августе.
Сторож оплат перепроверяет их постоянно, и эта колонка хранит, когда оплату видели в последний
раз, — случай пропажи называет окно «была — нет».

Revision ID: 0278_iiko_payment_seen_at
Revises: 0277_tax_intake_mail_uid_sha
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0278_iiko_payment_seen_at"
down_revision = "0277_tax_intake_mail_uid_sha"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "iiko_invoice_payment_push",
        sa.Column("iiko_seen_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("iiko_invoice_payment_push", "iiko_seen_at")

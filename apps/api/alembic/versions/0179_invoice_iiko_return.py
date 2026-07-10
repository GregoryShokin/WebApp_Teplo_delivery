"""iiko-возврат при коррекции оплаченной накладной (Фаза 2).

Оплаченный iiko-документ править нельзя, поэтому коррекцию отражаем отдельным документом
returned_invoice (возврат лишних товаров со ссылкой на incomingInvoiceId → дебиторка в iiko).
Колонки статуса на supplier_invoice зеркалят iiko_push_*: external_id/status/error + JSONB с
позициями возврата, ждущими проводки/ретрая.

Revision ID: 0179_invoice_iiko_return
Revises: 0178_invoice_edit_paid
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0179_invoice_iiko_return"
down_revision = "0178_invoice_edit_paid"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "supplier_invoice",
        sa.Column("iiko_return_external_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "supplier_invoice",
        sa.Column(
            "iiko_return_status",
            sa.String(length=24),
            nullable=False,
            server_default="none",
        ),
    )
    op.add_column(
        "supplier_invoice",
        sa.Column("iiko_return_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "supplier_invoice",
        sa.Column(
            "iiko_return_lines",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("supplier_invoice", "iiko_return_lines")
    op.drop_column("supplier_invoice", "iiko_return_error")
    op.drop_column("supplier_invoice", "iiko_return_status")
    op.drop_column("supplier_invoice", "iiko_return_external_id")

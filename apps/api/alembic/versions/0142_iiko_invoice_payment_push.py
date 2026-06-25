"""Идемпотентность/аудит зеркалирования оплаты накладной в iiko (Cloud add_payment).

Таблица ``iiko_invoice_payment_push`` фиксирует факт отправки оплаты iiko-накладной в iiko
(``/api/inventory/v1/incoming_invoice/modify/add_payment``): один наш оплаченный платёж →
один пуш (``idempotency_key``, обычно ``invoice:<id>``), статус ok/error, счётчик попыток
для ретраев сверочным джобом без бесконечного долбления iiko.

Revision ID: 0142_iiko_invoice_payment_push
Revises: 0141_kassa_manual_pending
Create Date: 2026-06-26
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0142_iiko_invoice_payment_push"
down_revision = "0141_kassa_manual_pending"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "iiko_invoice_payment_push",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("account_to", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("iiko_document_id", sa.String(length=128), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "request_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "response_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["invoice_id"], ["supplier_invoice.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_iiko_invoice_payment_push_key"),
    )
    op.create_index(
        "ix_iiko_invoice_payment_push_invoice", "iiko_invoice_payment_push", ["invoice_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_iiko_invoice_payment_push_invoice", table_name="iiko_invoice_payment_push")
    op.drop_table("iiko_invoice_payment_push")

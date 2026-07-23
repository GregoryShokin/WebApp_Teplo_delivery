"""Налоги: staging входящих документов бухгалтера (платёжки, ведомости).

Зеркалит email_invoice_intake: сырое вложение + результат разбора + статус, дедуп по SHA-256.
Разбор держится под human-контролем — распознавание по рукописному имени файла не уходит
в расчёт молча, а ждёт продвижения.

Revision ID: 0209_tax_document_intake
Revises: 0208_taxes
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0209_tax_document_intake"
down_revision = "0208_taxes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tax_document_intake",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mailbox", sa.String(length=32), nullable=False),
        sa.Column("from_addr", sa.String(length=320), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("message_id", sa.String(length=512), nullable=True),
        sa.Column("message_uid", sa.String(length=64), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("filename", sa.String(length=512), nullable=True),
        sa.Column("mime", sa.String(length=128), nullable=True),
        sa.Column("attachment_sha256", sa.String(length=64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=True),
        sa.Column(
            "document_type", sa.String(length=24), nullable=False, server_default="unknown"
        ),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="needs_review"
        ),
        sa.Column(
            "recognition", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("tax_payment_bundle_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tax_document_intake"),
        sa.UniqueConstraint("attachment_sha256", name="uq_tax_document_intake_sha"),
        sa.CheckConstraint(
            "status in ('parsed', 'needs_review', 'promoted', 'unsupported', 'error', 'ignored')",
            name="ck_tax_document_intake_status",
        ),
        sa.CheckConstraint(
            "document_type in ('payment_order', 'payroll_statement', 'unknown')",
            name="ck_tax_document_intake_type",
        ),
    )
    op.create_index(
        "ix_tax_document_intake_status", "tax_document_intake", ["status"]
    )
    op.create_index(
        "ix_tax_document_intake_received", "tax_document_intake", ["received_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_tax_document_intake_received", table_name="tax_document_intake")
    op.drop_index("ix_tax_document_intake_status", table_name="tax_document_intake")
    op.drop_table("tax_document_intake")

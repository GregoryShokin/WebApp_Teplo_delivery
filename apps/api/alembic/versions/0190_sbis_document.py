"""SBIS (Saby) EDO incoming-document mirror for reconciliation against iiko invoices.

SBIS is NOT a payment source (owner decision 2026-07-16): payables keep coming from the
iiko sync; EDO documents are pulled into ``sbis_document`` so unmatched ones («пришло по
ЭДО, но нет в iiko») can be surfaced to the operator.

Revision ID: 0190_sbis_document
Revises: 0189_payroll_draft_deleted
Create Date: 2026-07-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0190_sbis_document"
down_revision = "0189_payroll_draft_deleted"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sbis_document",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("sbis_doc_id", sa.String(64), nullable=False),
        sa.Column("doc_type", sa.String(64), nullable=True),
        sa.Column("regulation", sa.String(128), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("number", sa.String(128), nullable=True),
        sa.Column("doc_date", sa.Date(), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("amount_wo_vat", sa.Numeric(14, 2), nullable=True),
        sa.Column("counterparty_name", sa.String(256), nullable=True),
        sa.Column("counterparty_inn", sa.String(12), nullable=True),
        sa.Column("state_code", sa.String(16), nullable=True),
        sa.Column("state_name", sa.String(128), nullable=True),
        sa.Column("attachment_kind", sa.String(64), nullable=True),
        sa.Column("link_cabinet", sa.Text(), nullable=True),
        sa.Column("link_pdf", sa.Text(), nullable=True),
        sa.Column("link_xml", sa.Text(), nullable=True),
        sa.Column(
            "match_status", sa.String(16), nullable=False, server_default="unmatched"
        ),
        sa.Column(
            "matched_invoice_id",
            UUID(as_uuid=True),
            sa.ForeignKey("supplier_invoice.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("match_note", sa.String(64), nullable=True),
        sa.Column("raw", JSONB(), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_synced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("uq_sbis_document_doc_id", "sbis_document", ["sbis_doc_id"], unique=True)
    op.create_index("ix_sbis_document_match_status", "sbis_document", ["match_status"])
    op.create_index("ix_sbis_document_invoice", "sbis_document", ["matched_invoice_id"])
    op.create_index("ix_sbis_document_date", "sbis_document", ["doc_date"])


def downgrade() -> None:
    op.drop_table("sbis_document")

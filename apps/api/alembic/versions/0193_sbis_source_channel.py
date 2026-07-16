"""SBIS as an invoice SOURCE for service suppliers (owner decisions 2026-07-16).

Adds the 'sbis' collection channel and invoice source, plus routing columns on
``sbis_document``: resolved counterparty, materialized invoice link and the intake
status (mirror / new_counterparty / materialized / duplicate). Producers stay in
mirror mode; only counterparties with an explicit 'sbis' collection source get their
documents materialized into payable invoices.

Revision ID: 0193_sbis_source_channel
Revises: 0192_sbis_document
Create Date: 2026-07-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0193_sbis_source_channel"
down_revision = "0192_sbis_document"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PG16: ADD VALUE допустим в транзакции, пока новое значение не используется в ней же.
    op.execute("ALTER TYPE counterparty_invoice_source ADD VALUE IF NOT EXISTS 'sbis'")
    op.execute("ALTER TYPE counterparty_collection_source_kind ADD VALUE IF NOT EXISTS 'sbis'")

    op.add_column(
        "sbis_document",
        sa.Column(
            "counterparty_id",
            UUID(as_uuid=True),
            sa.ForeignKey("counterparty.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "sbis_document",
        sa.Column(
            "invoice_id",
            UUID(as_uuid=True),
            sa.ForeignKey("supplier_invoice.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "sbis_document",
        sa.Column("intake_status", sa.String(24), nullable=False, server_default="mirror"),
    )
    op.create_index("ix_sbis_document_counterparty", "sbis_document", ["counterparty_id"])
    op.create_index("ix_sbis_document_intake_status", "sbis_document", ["intake_status"])


def downgrade() -> None:
    op.drop_index("ix_sbis_document_intake_status", table_name="sbis_document")
    op.drop_index("ix_sbis_document_counterparty", table_name="sbis_document")
    op.drop_column("sbis_document", "intake_status")
    op.drop_column("sbis_document", "invoice_id")
    op.drop_column("sbis_document", "counterparty_id")
    # Значения enum'ов не удаляем: PostgreSQL не поддерживает DROP VALUE, и это безвредно.

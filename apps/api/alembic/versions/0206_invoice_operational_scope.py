"""Separate warehouse documents from finance-only bills and service closings.

Revision ID: 0206_invoice_operational_scope
Revises: 0205_payroll_payout_booking
Create Date: 2026-07-21
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0206_invoice_operational_scope"
down_revision = "0205_payroll_payout_booking"
branch_labels = None
depends_on = None

_INVOICE = "supplier_invoice"
SCOPE = postgresql.ENUM(
    "warehouse",
    "finance",
    "unknown",
    name="supplier_invoice_operational_scope",
)


def upgrade() -> None:
    SCOPE.create(op.get_bind(), checkfirst=True)
    op.add_column(
        _INVOICE,
        sa.Column(
            "operational_scope",
            postgresql.ENUM(name="supplier_invoice_operational_scope", create_type=False),
            nullable=False,
            server_default="unknown",
        ),
    )

    # Каналы с однозначной операционной семантикой.
    op.execute(
        "UPDATE supplier_invoice SET operational_scope = 'warehouse' "
        "WHERE source::text IN ('iiko', 'kassa_cheque', 'kassa_invoice')"
    )
    op.execute(
        "UPDATE supplier_invoice SET operational_scope = 'finance' "
        "WHERE source::text IN ('email', 'telegram', 'sbis')"
    )

    # Ручной складской документ всегда несёт время приёмки, строки, iiko documentId либо
    # след отправки. Остальные historical manual — проверенные финансовые УПД/акты,
    # созданные из реестра контрагентов без товарного состава.
    op.execute(
        "UPDATE supplier_invoice si SET operational_scope = 'warehouse' "
        "WHERE si.source::text = 'manual' AND ("
        "si.issued_at IS NOT NULL OR si.external_id IS NOT NULL "
        "OR si.iiko_push_status <> 'not_pushed' "
        "OR EXISTS (SELECT 1 FROM invoice_line_item li WHERE li.invoice_id = si.id)"
        ")"
    )
    op.execute(
        "UPDATE supplier_invoice SET operational_scope = 'finance' "
        "WHERE source::text = 'manual' AND operational_scope::text = 'unknown'"
    )
    op.create_index(
        "ix_supplier_invoice_operational_scope",
        _INVOICE,
        ["operational_scope"],
    )


def downgrade() -> None:
    op.drop_index("ix_supplier_invoice_operational_scope", table_name=_INVOICE)
    op.drop_column(_INVOICE, "operational_scope")
    SCOPE.drop(op.get_bind(), checkfirst=True)

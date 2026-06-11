"""counterparty brand routing rules

Маршрутизация одного iiko-поставщика (бренда) на несколько юрлиц по префиксу
номера документа: ТРКА→ООО «ТОРА», 0ЭКА→ИП Скачкова. Префикс = transportInvoiceNumber
до дефиса.

Revision ID: 0098_cp_routing_rule
Revises: 0097_cp_invoice_vat
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0098_cp_routing_rule"
down_revision = "0097_cp_invoice_vat"
branch_labels = None
depends_on = None

_TABLE = "counterparty_routing_rule"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("iiko_supplier_guid", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=64), nullable=False),
        sa.Column("counterparty_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["counterparty_id"], ["counterparty.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("iiko_supplier_guid", "prefix", name="uq_routing_rule_guid_prefix"),
    )
    op.create_index("ix_routing_rule_guid", _TABLE, ["iiko_supplier_guid"])


def downgrade() -> None:
    op.drop_index("ix_routing_rule_guid", table_name=_TABLE)
    op.drop_table(_TABLE)

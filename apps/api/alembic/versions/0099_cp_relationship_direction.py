"""counterparty relationship + invoice direction

Тип контрагента по способу расчёта: official (перевод) / informal (карта-нал) /
barter (двусторонний — приходные AP + доходные AR, ведём сальдо). Накладной добавлено
направление: payable (iiko incomingInvoice, мы должны) / receivable (iiko
outgoingInvoice, бартер-партнёр должен нам). Дефолты: relationship=official,
direction=payable; barter проставляется автоматически при синке доходных накладных.

Revision ID: 0099_cp_relationship_direction
Revises: 0098_cp_routing_rule
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0099_cp_relationship_direction"
down_revision = "0098_cp_routing_rule"
branch_labels = None
depends_on = None

_PROFILE = "counterparty_payable_profile"
_INVOICE = "supplier_invoice"

RELATIONSHIP = postgresql.ENUM(
    "official", "informal", "barter", name="counterparty_relationship"
)
DIRECTION = postgresql.ENUM("payable", "receivable", name="supplier_invoice_direction")


def upgrade() -> None:
    RELATIONSHIP.create(op.get_bind(), checkfirst=True)
    DIRECTION.create(op.get_bind(), checkfirst=True)
    op.add_column(
        _PROFILE,
        sa.Column(
            "relationship",
            postgresql.ENUM(name="counterparty_relationship", create_type=False),
            nullable=False,
            server_default="official",
        ),
    )
    op.add_column(
        _INVOICE,
        sa.Column(
            "direction",
            postgresql.ENUM(name="supplier_invoice_direction", create_type=False),
            nullable=False,
            server_default="payable",
        ),
    )
    op.create_index("ix_supplier_invoice_direction", _INVOICE, ["direction"])


def downgrade() -> None:
    op.drop_index("ix_supplier_invoice_direction", table_name=_INVOICE)
    op.drop_column(_INVOICE, "direction")
    op.drop_column(_PROFILE, "relationship")
    DIRECTION.drop(op.get_bind(), checkfirst=True)
    RELATIONSHIP.drop(op.get_bind(), checkfirst=True)

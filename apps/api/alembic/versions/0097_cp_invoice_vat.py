"""supplier invoice VAT breakdown

НДС по ставкам на накладной. ``vat_breakdown`` (JSONB rate->amount) считается из
позиций iiko (vatPercent/vatSum) и попадает в назначение платежа («в т.ч. НДС
10% - X; 22% - Y»). НДС входит в gross ``amount`` — это информационная разбивка.

Revision ID: 0097_cp_invoice_vat
Revises: 0096_cp_profile_terms
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0097_cp_invoice_vat"
down_revision = "0096_cp_profile_terms"
branch_labels = None
depends_on = None

_TABLE = "supplier_invoice"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("vat_total", sa.Numeric(14, 2), nullable=False, server_default="0"),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "vat_breakdown",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "vat_breakdown")
    op.drop_column(_TABLE, "vat_total")

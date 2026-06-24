"""Тумбстоны накладных: помеченные удалёнными iiko-документы не пере-импортируются.

Обратный синк апсертит iiko-накладные по external_id (id документа iiko). Если мы
жёстко удалили накладную, которая всё ещё PROCESSED в iiko и попадает в окно синка,
следующий прогон создаст её заново. Таблица tombstone (source, external_id) велит синку
пропускать такой документ навсегда — ключ это iiko-id, поэтому он переживает удаление
строки накладной.

Revision ID: 0137_supplier_invoice_tombstone
Revises: 0136_kassa_invoice_push
Create Date: 2026-06-24
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0137_supplier_invoice_tombstone"
down_revision = "0136_kassa_invoice_push"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "supplier_invoice_tombstone" in inspector.get_table_names():
        return
    op.create_table(
        "supplier_invoice_tombstone",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="iiko"),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("source", "external_id", name="uq_supplier_invoice_tombstone"),
    )


def downgrade() -> None:
    op.drop_table("supplier_invoice_tombstone")

"""Чек-пойнт акта разницы в цепочке исправления оплаченного чека.

Решение владельца 23.10.2026: исправление оплаченной накладной — сторно исходной, новая
«(испр.)» и оплата на неё без второй траты денег. Разница чека, ушедшая в расходные статьи
(питание персонала), проводится актом услуг от того же поставщика; id акта хранится здесь,
чтобы ретрай саги не завёл второй акт.

Revision ID: 0279_correction_act_external_id
Revises: 0278_iiko_payment_seen_at
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0279_correction_act_external_id"
down_revision = "0278_iiko_payment_seen_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "supplier_invoice",
        sa.Column("iiko_correction_act_external_id", sa.String(128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("supplier_invoice", "iiko_correction_act_external_id")

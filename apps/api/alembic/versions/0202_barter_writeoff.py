"""Списание хвоста товарного долга: ``supplier_invoice.barter_writeoff_amount``.

Правило владельца (2026-07-19): вернули товаром МЕНЬШЕ выданного — остаток можно либо оставить
висеть долгом, либо СПИСАТЬ («вернули 3 кг вместо 3,06 — про эти 60 грамм просто забываю»).
Списанная сумма входит в зачётную стоимость займа, поэтому заём закрывается штатно
(``returned``), а его кредиторка не остаётся вечным огрызком в пару десятков рублей.

Зеркальный случай — перевозврат — поля не требует: долг займа номинирован товаром и при
возврате большего количества закрывается ровно суммой строки (лишние граммы просто уходят по
более низкой цене за кг), дебиторки не возникает.

Revision ID: 0202_barter_writeoff
Revises: 0201_fixed_assets
Create Date: 2026-07-19
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0202_barter_writeoff"
down_revision = "0201_fixed_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "supplier_invoice",
        sa.Column(
            "barter_writeoff_amount",
            sa.Numeric(14, 2),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("supplier_invoice", "barter_writeoff_amount")

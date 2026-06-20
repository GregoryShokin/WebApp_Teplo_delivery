"""kassa_invoice source enum value

Добавляет ``kassa_invoice`` в enum ``counterparty_invoice_source``. Накладные,
созданные через страницу Касса, помечаются этим source (вкладка «Накладные» Кассы
фильтрует по нему; на Складе видны все). ``ADD VALUE`` нельзя в транзакции —
autocommit-блок (паттерн из 0118).

Revision ID: 0128_kassa_invoice_source
Revises: 0127_courier_substitution
Create Date: 2026-06-20
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0128_kassa_invoice_source"
down_revision = "0127_courier_substitution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            sa.text(
                "ALTER TYPE counterparty_invoice_source ADD VALUE IF NOT EXISTS 'kassa_invoice'"
            )
        )


def downgrade() -> None:
    # PostgreSQL не поддерживает DROP VALUE из enum — значение остаётся.
    pass

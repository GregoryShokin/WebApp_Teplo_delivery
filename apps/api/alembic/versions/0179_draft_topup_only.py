"""Черновик-пополнение Сейфа без резерва: флаг ``topup_only``.

Внутренний перевод банк→Сейф из «Нового платежа» создаёт банковский черновик на карту ИП,
как свободный вывод, но при оплате Сейф ПОПОЛНЯЕТСЯ БЕЗ резерва (в отличие от свободного
вывода, где на оплате заводится целёвка). Отличается флагом ``topup_only``: paid-переход
книжит только транзит р/с→Сейф и на этом останавливается.

Revision ID: 0179_draft_topup_only
Revises: 0178_single_transfer_article
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0179_draft_topup_only"
down_revision = "0178_single_transfer_article"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "counterparty_payment_draft",
        sa.Column(
            "topup_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("counterparty_payment_draft", "topup_only")

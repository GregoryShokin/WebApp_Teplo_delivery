"""Counterparty origin: where the card came from (iiko / manual / email / sbis).

Owner request 2026-07-16: the registry must visually distinguish counterparties pulled
from iiko, created manually, or discovered via EDO/mail. Backfill: iiko alias → 'iiko',
email collection source → 'email', the rest → 'manual' (SBIS placeholders start after
this migration, so none exist yet).

Revision ID: 0194_counterparty_origin
Revises: 0193_sbis_source_channel
Create Date: 2026-07-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0194_counterparty_origin"
down_revision = "0193_sbis_source_channel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("counterparty", sa.Column("origin", sa.String(16), nullable=True))
    op.execute(
        "UPDATE counterparty SET origin='iiko' WHERE id IN "
        "(SELECT DISTINCT counterparty_id FROM counterparty_alias WHERE source='iiko')"
    )
    op.execute(
        "UPDATE counterparty SET origin='email' WHERE origin IS NULL AND id IN "
        "(SELECT DISTINCT counterparty_id FROM counterparty_collection_source WHERE kind='email')"
    )
    # Заглушки, уже созданные СБИС-синком до этой миграции (0193 вышла раньше).
    op.execute(
        "UPDATE counterparty SET origin='sbis' WHERE origin IS NULL AND id IN "
        "(SELECT DISTINCT counterparty_id FROM sbis_document WHERE counterparty_id IS NOT NULL)"
    )
    op.execute("UPDATE counterparty SET origin='manual' WHERE origin IS NULL")


def downgrade() -> None:
    op.drop_column("counterparty", "origin")

"""Единая статья «Внутренний перевод» вместо двух технических.

Все двухногие переводы (снятие с Сейфа, передача в кассу, транзиты банк→Сейф,
банк-классификация выписки, внутренний перевод) теперь книжатся ОДНОЙ статьёй
``internal_transfer`` (movement_type=internal) — направление проводки (in/out) само
различает поступление/списание. Две технические статьи «Выбытие/Поступление — Перевод
между счетами» архивируются (is_active=False): из каталога/пикеров уходят, историческая
привязка проводок по id сохраняется.

Revision ID: 0178_single_transfer_article
Revises: 0177_advance_source_operation
Create Date: 2026-07-10
"""

from __future__ import annotations

from alembic import op

revision = "0178_single_transfer_article"
down_revision = "0177_advance_source_operation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE dds_articles SET is_active = false "
        "WHERE code IN ('vybytie_perevod_mezhdu_schetami', "
        "'postuplenie_perevod_mezhdu_schetami')"
    )
    # Единая статья точно активна.
    op.execute("UPDATE dds_articles SET is_active = true WHERE code = 'internal_transfer'")


def downgrade() -> None:
    op.execute(
        "UPDATE dds_articles SET is_active = true "
        "WHERE code IN ('vybytie_perevod_mezhdu_schetami', "
        "'postuplenie_perevod_mezhdu_schetami')"
    )

"""Схождение двух веток миграций: происхождение ОС и контур расчётов.

Обе ветки выросли из ``0231_repair_articles_named`` параллельно и разошлись номером 0232:
на проде живёт ``0232_asset_acquisition_source`` (происхождение объекта ОС), в этой ветке —
``0232_settlement_ledger_profile`` и всё, что на ней построено вплоть до 0238.

Две головы alembic — это не косметика: ``alembic upgrade head`` при них падает с ошибкой
«Multiple head revisions are present», то есть выкатка ветки на прод невозможна физически,
пока головы не сведены. Схему эта ревизия не трогает — она только объявляет, что обе линии
дальше идут одной.

Revision ID: 0239_merge_asset_and_settlement
Revises: 0238_retire_legacy_toggles, 0232_asset_acquisition_source
Create Date: 2026-08-02
"""

from __future__ import annotations

revision = "0239_merge_asset_and_settlement"
down_revision = ("0238_retire_legacy_toggles", "0232_asset_acquisition_source")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

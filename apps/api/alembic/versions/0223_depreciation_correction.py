"""Ручная коррекция начисленной амортизации: след правки и её автор.

Закрытие месяца становится автоматическим (планировщик 1-го числа), а значит нужен способ
поправить машину, когда она ошиблась: объект ввели не тем числом, стоимость уточнили, срок
пересмотрели. Сейчас поправить нельзя вообще, и мешают этому три вещи сразу.

Уникальность ``(asset_id, period_month)`` не даёт положить вторую строку за месяц, а
``amount >= 0`` не даёт положить отрицательную сторнирующую. Классическое сторно отдельной
проводкой в этой схеме невозможно.

Ломать обе защиты было бы дорого: уникальность — единственное, что делает повторный прогон
закрытия месяца безопасным, а на неё завязана идемпотентность ночной джобы. Поэтому коррекция
здесь не отдельная проводка, а ПРАВКА строки месяца. Одна строка на объект и месяц остаётся
инвариантом, а история правки живёт в самой строке.

Третья причина — ``residual_after``: остаток хранится, а не считается на лету (чтобы баланс не
пересчитывал всю историю). Правка суммы за август делает остатки сентября и всех следующих
месяцев враньём. Поэтому коррекция обязана пересчитывать хвост, и делает это сервис
``correct_depreciation``; колонки ниже нужны, чтобы после пересчёта было видно, что цифра
не машинная.

Признак ``is_manual`` — не украшение: без него нельзя отличить ошибку расчёта от осознанной
правки владельца, а значит нельзя безопасно перезапустить закрытие месяца.

Revision ID: 0223_depreciation_correction
Revises: 0222_fixed_asset_threshold_10k
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0223_depreciation_correction"
down_revision = "0222_fixed_asset_threshold_10k"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "depreciation_entry",
        sa.Column(
            "is_manual", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "depreciation_entry",
        sa.Column("corrected_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_depreciation_entry_corrected_by",
        "depreciation_entry",
        "user",
        ["corrected_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "depreciation_entry",
        sa.Column("corrected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("depreciation_entry", sa.Column("note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("depreciation_entry", "note")
    op.drop_column("depreciation_entry", "corrected_at")
    op.drop_constraint(
        "fk_depreciation_entry_corrected_by", "depreciation_entry", type_="foreignkey"
    )
    op.drop_column("depreciation_entry", "corrected_by_user_id")
    op.drop_column("depreciation_entry", "is_manual")

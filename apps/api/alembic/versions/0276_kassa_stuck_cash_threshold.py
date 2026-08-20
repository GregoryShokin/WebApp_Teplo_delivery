"""Касса: порог «зависшей налички» — сигнал о забытой инкассации.

Настройка ``kassa.stuck_cash_threshold_rub`` (дефолт 1000 ₽): сколько рублей сверх
стартового флоута может остаться в ящике ЗАКРЫТОЙ смены, прежде чем витрина пометит
смену как «инкассацию забыли / инкассировали не всю наличку».

Порог в рублях, а не в процентах выручки (в отличие от ``shortage_penalty_threshold_pct``):
зависшая наличка — это абсолютная сумма, не доехавшая в Главную кассу. Дефолт 1000 выбран
по факту: за месяц до 19.08.2026 обычный «хвост» в ящике держался в пределах 265–914 ₽
(округление размена), а пропущенная инкассация 19.08 дала 2 821,83 ₽.

Новых прав не заводит: сигнал — вычисляемое поле витрины смены под ``kassa.shifts.read``.

Revision ID: 0276_kassa_stuck_cash_threshold
Revises: 0275_payment_draft_vat
Create Date: 2026-08-20
"""

from __future__ import annotations

from alembic import op

revision = "0276_kassa_stuck_cash_threshold"
down_revision = "0275_payment_draft_vat"
branch_labels = None
depends_on = None

SETTING_ID = "3a6f5c9d-2e71-4b83-9c0a-5d4e7f8a1b62"
SETTING_KEY = "kassa.stuck_cash_threshold_rub"


def upgrade() -> None:
    op.execute(
        f"""
        INSERT INTO app_setting (
            id, key, value, value_type, category, display_name,
            description, widget_type, widget_options, unit, updated_at
        )
        SELECT '{SETTING_ID}', '{SETTING_KEY}', '1000'::jsonb, 'number',
               'kassa', 'Порог зависшей налички в кассе',
               'Смена помечается «инкассацию забыли», если после закрытия в ящике осталось '
               'больше этой суммы сверх стартового флоута. Меньше порога — обычный остаток '
               'размена, сигнала нет.',
               'number', null, '₽', now()
        WHERE NOT EXISTS (SELECT 1 FROM app_setting WHERE key = '{SETTING_KEY}')
        """
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM app_setting WHERE key = '{SETTING_KEY}'")

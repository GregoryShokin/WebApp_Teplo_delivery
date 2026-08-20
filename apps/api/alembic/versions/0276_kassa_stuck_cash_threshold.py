"""Касса: норма размена и порог «зависшей налички» — сигнал о забытой инкассации.

Две настройки, обе в рублях (в отличие от процентного ``shortage_penalty_threshold_pct``:
зависшая наличка — абсолютная сумма, не доехавшая в Главную кассу):

1. ``kassa.cash_float_norm_rub`` (дефолт 5000) — сколько наличных положено оставлять в
   денежном ящике на размен. За два месяца прода (61 смена, 20.06–19.08.2026) ящик
   открывался ровно с 5 000 с копейками 35 раз — это фактическая практика, а не догадка.
2. ``kassa.stuck_cash_threshold_rub`` (дефолт 1000) — насколько выше нормы смена может
   закрыться без сигнала. Обычный хвост размена держался в 265–914 ₽, а пропущенная
   инкассация 19.08.2026 дала 2 821,83 ₽.

Почему меряем от нормы, а не от остатка на открытие смены (решение владельца 20.08.2026):
разностное правило ошибается в обе стороны. 08.08.2026 ящик просел до 1 709 ₽ и за смену
был поднят обратно до нормы — прибавка 3 291,50 ₽ дала бы ложную тревогу при честной
инкассации на 13 390 ₽. И наоборот, хвост по 265–847 ₽ каждый день (12–18.08) не пробивал
порог ни разу, хотя ящик за неделю распух с 3 426 до 5 000 ₽.

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

NORM_SETTING_ID = "6c2b81f4-9d13-4a75-b60e-27c9f5e18a34"
NORM_SETTING_KEY = "kassa.cash_float_norm_rub"

THRESHOLD_SETTING_ID = "3a6f5c9d-2e71-4b83-9c0a-5d4e7f8a1b62"
THRESHOLD_SETTING_KEY = "kassa.stuck_cash_threshold_rub"


def upgrade() -> None:
    op.execute(
        f"""
        INSERT INTO app_setting (
            id, key, value, value_type, category, display_name,
            description, widget_type, widget_options, unit, updated_at
        )
        SELECT '{NORM_SETTING_ID}', '{NORM_SETTING_KEY}', '5000'::jsonb, 'number',
               'kassa', 'Норма размена в кассе',
               'Сколько наличных положено оставлять в денежном ящике на размен. От этой '
               'суммы считается, сколько выручки не доехало в Главную кассу: всё, что выше '
               'нормы, должно было уйти инкассацией.',
               'number', null, '₽', now()
        WHERE NOT EXISTS (SELECT 1 FROM app_setting WHERE key = '{NORM_SETTING_KEY}')
        """
    )
    op.execute(
        f"""
        INSERT INTO app_setting (
            id, key, value, value_type, category, display_name,
            description, widget_type, widget_options, unit, updated_at
        )
        SELECT '{THRESHOLD_SETTING_ID}', '{THRESHOLD_SETTING_KEY}', '1000'::jsonb, 'number',
               'kassa', 'Порог зависшей налички в кассе',
               'Смена помечается «инкассацию забыли», если после закрытия в ящике осталось '
               'больше этой суммы сверх нормы размена. Меньше порога — обычный остаток '
               'размена, сигнала нет.',
               'number', null, '₽', now()
        WHERE NOT EXISTS (SELECT 1 FROM app_setting WHERE key = '{THRESHOLD_SETTING_KEY}')
        """
    )


def downgrade() -> None:
    op.execute(
        f"DELETE FROM app_setting WHERE key in ('{NORM_SETTING_KEY}', '{THRESHOLD_SETTING_KEY}')"
    )

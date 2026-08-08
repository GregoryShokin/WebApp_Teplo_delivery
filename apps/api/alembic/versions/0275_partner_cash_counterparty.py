"""Кто считается партнёром по наличным из смены («В гостях у Алисы»).

ЗАЧЕМ НАСТРОЙКА. Партнёр забирает наличные прямо из смены — в iiko это изъятие на счёт
«Алиса наличные», и проводки ДДС оно намеренно не порождает: деньги ушли в дебиторку, а не в
расход. Возвращает партнёр внесением в кассу, и единственный надёжный ключ возврата — контрагент
на проводке: статья «Поступление денег с торг. точек» несёт весь поток инкассаций с точек
(на копии прода — 452 проводки), и считать гашение по ней значило бы вычитать из долга партнёра
обычную выручку.

Контрагент на внесении проставляется автоматически из пресета, но какой именно контрагент
считается ПАРТНЁРОМ — знание отчёта, а не кассы. Отсюда настройка: пока она пуста, баланс
показывает начисление и честно говорит, что долг завышен на непривязанные возвраты.

Значение — идентификатор контрагента строкой; пустая строка означает «партнёр не объявлен».

Revision ID: 0275_partner_cash_cp
Revises: 0274_balance_snapshot
Create Date: 2026-08-08
"""

from __future__ import annotations

from alembic import op

revision = "0275_partner_cash_cp"
down_revision = "0274_balance_snapshot"
branch_labels = None
depends_on = None

SETTING_ID = "b3f4c5d6-7a8b-4c9d-9e0f-1a2b3c4d5e6f"
SETTING_KEY = "kassa.partner_cash_counterparty_id"


def upgrade() -> None:
    op.execute(
        f"""
        INSERT INTO app_setting (
            id, key, value, value_type, category, display_name,
            description, widget_type, widget_options, unit, updated_at
        )
        SELECT '{SETTING_ID}', '{SETTING_KEY}', '""'::jsonb, 'string',
               'kassa', 'Контрагент партнёра по наличным из смены',
               'Идентификатор контрагента, чьи внесения в кассу гасят долг по наличным, '
               'изъятым из смены («В гостях у Алисы»). Пока не заполнено, баланс показывает '
               'начисление без вычета возвратов и поднимает предупреждение. Этого же '
               'контрагента нужно привязать к пресету внесения в «Настройках → Касса».',
               'text', null, null, now()
        WHERE NOT EXISTS (SELECT 1 FROM app_setting WHERE key = '{SETTING_KEY}')
        """
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM app_setting_history USING app_setting WHERE app_setting_history.setting_id = app_setting.id AND app_setting.key = '{SETTING_KEY}'")  # noqa: E501
    op.execute(f"DELETE FROM app_setting WHERE key = '{SETTING_KEY}'")

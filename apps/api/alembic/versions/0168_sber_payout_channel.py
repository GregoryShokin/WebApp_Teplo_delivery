"""Сбер как банк-плательщик payout-черновиков.

Добавляет колонку ``bank_provider`` в ``payroll_bank_draft`` и ``salary_advance_bank_draft``
(по ней на статусе «оплачен» книжится транзит банк→Сейф нужного банка) и настройку
``payroll.sber_payer_requisites`` — реквизиты НАШЕГО расчётного счёта в Сбере как плательщика
рублёвого РПП (`POST /v1/payments`, блок payer*). Получатель-Сейф общий (0153).

Revision ID: 0168_sber_payout_channel
Revises: 0167_sber_safe_payout_requisites
Create Date: 2026-07-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0168_sber_payout_channel"
down_revision = "0167_sber_safe_payout_requisites"
branch_labels = None
depends_on = None

SETTING_KEY = "payroll.sber_payer_requisites"

# Реквизиты ПЛАТЕЛЬЩИКА — наш расчётный счёт в Сбере (ИП Шокина К.Ю., Юго-Западный банк ПАО
# Сбербанк). Подтверждено владельцем 2026-07-04. КПП у ИП нет → "0".
SBER_PAYER_REQUISITES = """
{
    "payerName": "ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ ШОКИНА КРИСТИНА ЮРЬЕВНА",
    "payerInn": "890307589201",
    "payerKpp": "0",
    "payerAccount": "40802810252090056194",
    "payerBankBic": "046015602",
    "payerBankCorrAccount": "30101810600000000602"
}
"""


def upgrade() -> None:
    op.add_column(
        "payroll_bank_draft",
        sa.Column("bank_provider", sa.String(16), nullable=False, server_default="tbank"),
    )
    op.add_column(
        "salary_advance_bank_draft",
        sa.Column("bank_provider", sa.String(16), nullable=False, server_default="tbank"),
    )
    op.execute(
        sa.text(
            """
            insert into app_setting (
                id, key, value, value_type, category, display_name,
                description, widget_type, widget_options, unit, updated_at
            )
            values (
                'c4e1f2a3-5b6c-4d7e-8f90-1a2b3c4d5e6f',
                :key,
                cast(:value as jsonb),
                'object',
                'payroll',
                'Реквизиты плательщика Сбера для выплат',
                'Наш расчётный счёт в Сбере как плательщик банк-черновиков выплат '
                '(блок payer* для POST /v1/payments). Сверить с СберБизнес.',
                'json',
                null,
                null,
                now()
            )
            on conflict (key) do update set
                value = excluded.value,
                value_type = excluded.value_type,
                category = excluded.category,
                display_name = excluded.display_name,
                description = excluded.description,
                widget_type = excluded.widget_type,
                widget_options = excluded.widget_options,
                unit = excluded.unit,
                updated_at = excluded.updated_at
            """
        ).bindparams(key=SETTING_KEY, value=SBER_PAYER_REQUISITES)
    )


def downgrade() -> None:
    op.execute(sa.text("delete from app_setting where key = :key").bindparams(key=SETTING_KEY))
    op.drop_column("salary_advance_bank_draft", "bank_provider")
    op.drop_column("payroll_bank_draft", "bank_provider")

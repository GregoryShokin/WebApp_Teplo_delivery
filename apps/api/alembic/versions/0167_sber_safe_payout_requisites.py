"""Основные реквизиты Сейфа для банковских черновиков выплат.

Безналичные выплаты без собственных реквизитов получателя идут транзитом
банк -> Сейф. Для этого фиксируем счет физлица Шокиной К.Ю. в Сбере как
основной fallback для payroll/deposit/advance bank drafts.

Revision ID: 0167_sber_safe_payout_requisites
Revises: 0166_dismiss_deposit_period
Create Date: 2026-07-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0167_sber_safe_payout_requisites"
down_revision = "0166_dismiss_deposit_period"
branch_labels = None
depends_on = None

SETTING_KEY = "payroll.bank_payout_requisites"

# HISTORICAL, SUPERSEDED AND INCORRECT RECIPIENT DATA.
# Do not copy these values into runtime code or a new migration. Migration 0187
# restores the owner-approved T-Bank account and locks it in application code.
SBER_SAFE_REQUISITES = """
{
    "recipientName": "Шокина Кристина Юрьевна",
    "inn": "7707083893",
    "kpp": "616143002",
    "bankAcnt": "40817810552095257243",
    "bankBik": "046015602",
    "corrAccount": "30101810600000000602",
    "recipientCorrAccountNumber": "30101810600000000602",
    "executionOrder": 5,
    "paymentPurpose": "Перевод на Сейф под выплату за период {start}–{end}. НДС не облагается"
}
"""

PREVIOUS_PAYOUT_REQUISITES = """
{
    "recipientName": "ШОКИНА КРИСТИНА",
    "inn": "890307589201",
    "kpp": "0",
    "bankAcnt": "40817810800023540968",
    "bankBik": "044525974",
    "corrAccount": "30101810145250000974",
    "executionOrder": 5,
    "paymentPurpose": "Выплата заработной платы за период {start}–{end}"
}
"""


def upgrade() -> None:
    _upsert_requisites(
        value=SBER_SAFE_REQUISITES,
        display_name="Реквизиты Сейфа для выплат",
        description=(
            "Основные реквизиты физлица Шокиной К.Ю. в Сбере для банковских "
            "черновиков выплат без собственных реквизитов получателя."
        ),
    )


def downgrade() -> None:
    _upsert_requisites(
        value=PREVIOUS_PAYOUT_REQUISITES,
        display_name="Реквизиты выплаты ЗП",
        description="Реквизиты счёта ИП для одного банковского черновика на ведомость.",
    )


def _upsert_requisites(*, value: str, display_name: str, description: str) -> None:
    op.execute(
        sa.text(
            """
            insert into app_setting (
                id,
                key,
                value,
                value_type,
                category,
                display_name,
                description,
                widget_type,
                widget_options,
                unit,
                updated_at
            )
            values (
                '13d681d4-843f-43fd-9b57-8fc812253cd1',
                :key,
                cast(:value as jsonb),
                'object',
                'payroll',
                :display_name,
                :description,
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
        ).bindparams(
            key=SETTING_KEY,
            value=value,
            display_name=display_name,
            description=description,
        )
    )

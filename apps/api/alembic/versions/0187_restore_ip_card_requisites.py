"""Restore the owner-approved IP-card recipient requisites.

Revision ID: 0187_restore_ip_card_requisites
Revises: 0186_payroll_reserve_run_link
Create Date: 2026-07-14

This is a financial safety correction.  The recipient values below were supplied
and approved by the owner.  Do not change them in a migration, refactor, or bank-
provider task without an explicit owner request in that task.
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision = "0187_restore_ip_card_requisites"
down_revision = "0186_payroll_reserve_run_link"
branch_labels = None
depends_on = None

SETTING_KEY = "payroll.bank_payout_requisites"

# OWNER-APPROVED FINANCIAL CONSTANT. КПП is intentionally absent. The bank API
# adapter supplies protocol-required "0" for an IP/individual at request time.
OWNER_APPROVED_REQUISITES = {
    "recipientName": "Шокина Кристина Юрьевна",
    "inn": "890307589201",
    "bankAcnt": "40817810800023540968",
    "bankBik": "044525974",
    "bankName": 'АО "ТБанк"',
    "corrAccount": "30101810145250000974",
    "recipientCorrAccountNumber": "30101810145250000974",
    "executionOrder": 5,
    "paymentPurpose": ("Перевод на Сейф под выплату за период {start}–{end}. НДС не облагается"),
}


def upgrade() -> None:
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
                'Эталонные реквизиты карты ИП для выплат',
                'Зафиксированы владельцем. Не менять без его явной просьбы.',
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
            value=json.dumps(OWNER_APPROVED_REQUISITES, ensure_ascii=False),
        )
    )


def downgrade() -> None:
    # Data correction is intentionally irreversible: a downgrade must never
    # silently restore the superseded recipient and redirect real payments.
    pass

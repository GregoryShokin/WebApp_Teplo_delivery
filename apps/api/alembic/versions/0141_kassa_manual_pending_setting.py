"""add kassa manual pending cheque feature flag setting

Revision ID: 0141_kassa_manual_pending_setting
Revises: 0140_supplier_prepayment
Create Date: 2026-06-25
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0141_kassa_manual_pending_setting"
down_revision = "0140_supplier_prepayment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Флаг ручного ввода суммы чека (блок «Добавить сумму чека вручную» в выборе оплаты).
    # ВЫКЛЮЧЕН по умолчанию: карт-операции приходят вебхуком T-Банка за ~15–20 мин, поэтому
    # ручной pending нужен лишь как страховка на случай проблем с API — включается тумблером.
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
                'a3d9f1b2-6c84-4e57-9a21-2f7b5c0d8e44',
                'kassa.manual_pending_cheque_enabled',
                'false'::jsonb,
                'boolean',
                'kassa',
                'Ручной ввод суммы чека (ожидает подтверждения банком)',
                'Если включено, в выборе оплаты при создании чека появляется блок ручного '
                    || 'ввода суммы — на случай, когда банк ещё не передал операцию по карте '
                    || '(задержка/выходные); чек создаётся в статусе «ожидает подтверждения '
                    || 'банком» и сматчится с операцией позже. По умолчанию выключено: карт-'
                    || 'операции приходят вебхуком за ~15–20 минут.',
                'boolean',
                null,
                null,
                now()
            )
            on conflict (key) do nothing
            """
        )
    )


def downgrade() -> None:
    op.execute("delete from app_setting where key = 'kassa.manual_pending_cheque_enabled'")

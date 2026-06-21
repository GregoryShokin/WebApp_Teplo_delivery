"""Ручной чек Кассы с отложенным матчем (пендинг «Ожидает подтверждения банком»).

Два изменения под фичу «ввод суммы чека вручную, когда банк ещё не передал card-операцию»:

* в enum ``invoice_allocation_source`` добавляем значение ``card_pending`` — это аллокация
  ручной карточной части чека (покрывает сумму, но банк ещё не подтвердил; при матче
  заменяется обычной ``bank``);
* сидим настройку ``kassa.pending_cheque_alert_days`` (по умолчанию 4 дня) — через сколько
  дней без подтверждения банком чек поднимается в «Требует разбора». После подключения
  webhooks владелец ставит 1–2 в «Настройках приложения».

Revision ID: 0129_kassa_pending_cheque
Revises: 0128_kassa_invoice_source
Create Date: 2026-06-21
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0129_kassa_pending_cheque"
down_revision = "0128_kassa_invoice_source"
branch_labels = None
depends_on = None

SETTING_ID = uuid.UUID("c4e1a9f0-7b62-4d18-9a3e-2f5c8b1d6e40")
SETTING_KEY = "kassa.pending_cheque_alert_days"


def upgrade() -> None:
    # PG 12+: ADD VALUE допустимо в транзакции, пока значение не используется в ней же.
    op.execute("ALTER TYPE invoice_allocation_source ADD VALUE IF NOT EXISTS 'card_pending'")

    admin_user_id = op.get_bind().scalar(
        sa.text('select id from "user" order by created_at limit 1')
    )
    app_setting = sa.table(
        "app_setting",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("key", sa.String()),
        sa.column("value", postgresql.JSONB()),
        sa.column("value_type", sa.String()),
        sa.column("category", sa.String()),
        sa.column("display_name", sa.Text()),
        sa.column("description", sa.Text()),
        sa.column("widget_type", sa.Text()),
        sa.column("widget_options", postgresql.JSONB()),
        sa.column("unit", sa.Text()),
        sa.column("updated_by_user_id", postgresql.UUID(as_uuid=True)),
    )
    stmt = postgresql.insert(app_setting).values(
        id=SETTING_ID,
        key=SETTING_KEY,
        value=4,
        value_type="number",
        category="kassa",
        display_name="Чек: дней до «Требует разбора»",
        description=(
            "Через сколько дней без подтверждения банком ручной чек Кассы поднимается в "
            "«Требует разбора». Сейчас 4 (до подключения webhooks), после — 1–2."
        ),
        widget_type="number",
        widget_options=None,
        unit="дн.",
        updated_by_user_id=admin_user_id,
    )
    op.get_bind().execute(stmt.on_conflict_do_nothing(index_elements=["key"]))


def downgrade() -> None:
    op.execute(
        "delete from app_setting_history where setting_id in "
        f"(select id from app_setting where key = '{SETTING_KEY}')"
    )
    op.execute(f"delete from app_setting where key = '{SETTING_KEY}'")
    # Значение enum ``card_pending`` не удаляем: PostgreSQL не поддерживает DROP VALUE без
    # пересоздания типа; оставлять лишнее значение безопасно.

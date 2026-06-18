"""Целевой депозит старшего курьера — отдельная настройка.

Должность «Старший курьер» получает собственную целевую сумму депозита,
независимую от общего целевого депозита курьеров. Сидим настройку
couriers.deposit.target_amount_senior в app_setting (по умолчанию 5000 ₽,
владелец задаёт в «Исходные данные → Депозиты»).

Revision ID: 0124_courier_senior_target
Revises: 0123_kassa_cheque_iiko_payout
Create Date: 2026-06-18
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0124_courier_senior_target"
down_revision = "0123_kassa_cheque_iiko_payout"
branch_labels = None
depends_on = None

SETTING_ID = uuid.UUID("a7b3c1d2-4e5f-4a6b-8c9d-0e1f2a3b4c5d")
SETTING_KEY = "couriers.deposit.target_amount_senior"


def upgrade() -> None:
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
        value=5000,
        value_type="number",
        category="couriers",
        display_name="Целевой депозит старшего курьера",
        description="Целевая сумма депозита для должности «Старший курьер».",
        widget_type="number",
        widget_options=None,
        unit="₽",
        updated_by_user_id=admin_user_id,
    )
    op.get_bind().execute(stmt.on_conflict_do_nothing(index_elements=["key"]))


def downgrade() -> None:
    op.execute(
        "delete from app_setting_history where setting_id in "
        f"(select id from app_setting where key = '{SETTING_KEY}')"
    )
    op.execute(f"delete from app_setting where key = '{SETTING_KEY}'")

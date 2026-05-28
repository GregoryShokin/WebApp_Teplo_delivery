"""seed weekday payroll premium setting

Revision ID: 0012_weekday_payroll_premium
Revises: 0011_percent_methodology
Create Date: 2026-05-28
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0012_weekday_payroll_premium"
down_revision = "0011_percent_methodology"
branch_labels = None
depends_on = None

SETTING_KEY = "payroll.weekday_premium"
SETTING_VALUE = {"friday": 200, "saturday": 200}


def upgrade() -> None:
    conn = op.get_bind()
    admin_user_id = conn.scalar(sa.text('select id from "user" order by created_at limit 1'))
    existing_id = conn.scalar(
        sa.text("select id from app_setting where key = :key"),
        {"key": SETTING_KEY},
    )

    if existing_id is not None:
        conn.execute(
            sa.text(
                """
                update app_setting
                   set category = 'payroll',
                       display_name = :display_name,
                       description = :description,
                       widget_type = :widget_type,
                       widget_options = null,
                       unit = :unit
                 where key = :key
                """
            ),
            _setting_metadata(),
        )
        return

    setting_id = uuid.uuid4()
    app_setting_table = sa.table(
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
    op.bulk_insert(
        app_setting_table,
        [
            {
                "id": setting_id,
                "key": SETTING_KEY,
                "value": SETTING_VALUE,
                "value_type": "object",
                "category": "payroll",
                **_setting_metadata(),
                "widget_options": None,
                "updated_by_user_id": admin_user_id,
            }
        ],
    )

    app_setting_history_table = sa.table(
        "app_setting_history",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("setting_id", postgresql.UUID(as_uuid=True)),
        sa.column("old_value", postgresql.JSONB()),
        sa.column("new_value", postgresql.JSONB()),
        sa.column("changed_by_user_id", postgresql.UUID(as_uuid=True)),
    )
    op.bulk_insert(
        app_setting_history_table,
        [
            {
                "id": uuid.uuid4(),
                "setting_id": setting_id,
                "old_value": None,
                "new_value": SETTING_VALUE,
                "changed_by_user_id": admin_user_id,
            }
        ],
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "delete from app_setting_history "
            "where setting_id in (select id from app_setting where key = :key)"
        ),
        {"key": SETTING_KEY},
    )
    conn.execute(
        sa.text("delete from app_setting where key = :key"),
        {"key": SETTING_KEY},
    )


def _setting_metadata() -> dict[str, str]:
    return {
        "key": SETTING_KEY,
        "display_name": "Надбавка за смену по дням недели",
        "description": "Дополнительные суммы к ставке за смену в выбранные дни недели",
        "widget_type": "weekday_premium",
        "unit": "₽",
    }

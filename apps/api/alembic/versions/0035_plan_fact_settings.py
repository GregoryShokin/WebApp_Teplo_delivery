"""add plan fact warning threshold setting

Revision ID: 0035_plan_fact_settings
Revises: 0034_shift_cost_estimate
Create Date: 2026-06-01
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0035_plan_fact_settings"
down_revision = "0034_shift_cost_estimate"
branch_labels = None
depends_on = None


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
                'c6df1fa5-f11e-45a8-9b01-bf46b4431f04',
                'schedule.plan_fact_warning_threshold_pct',
                '3.0'::jsonb,
                'number',
                'schedule',
                'Порог отклонения план-факт',
                'Порог отклонения в процентах для подсветки план-факт сверки графика сотрудников.',
                'percent',
                null,
                '%',
                now()
            )
            on conflict (key) do nothing
            """
        )
    )


def downgrade() -> None:
    op.execute(
        "delete from app_setting where key = 'schedule.plan_fact_warning_threshold_pct'"
    )

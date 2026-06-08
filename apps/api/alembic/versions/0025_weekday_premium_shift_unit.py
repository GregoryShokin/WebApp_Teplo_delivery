"""store weekday premium as fixed shift setting

Revision ID: 0025_weekday_premium_shift_unit
Revises: 0022_pin_origin
Create Date: 2026-05-30
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0025_weekday_premium_shift_unit"
down_revision = "0022_pin_origin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            update app_setting
               set value = jsonb_build_object(
                       'amount',
                       case
                           when value ? 'amount' then value -> 'amount'
                           when value ? 'friday'
                                and (value ->> 'friday')::numeric > 0 then value -> 'friday'
                           when value ? 'saturday' then value -> 'saturday'
                           else '200'::jsonb
                       end,
                       'threshold_hours',
                       coalesce(
                           value -> 'threshold_hours',
                           widget_options -> 'threshold_hours',
                           '8.0'::jsonb
                       )
                   ),
                   widget_options = coalesce(widget_options, '{}'::jsonb)
                       || jsonb_build_object(
                           'threshold_hours',
                           coalesce(widget_options -> 'threshold_hours', '8.0'::jsonb)
                   ),
                   display_name = 'Надбавка пт/сб',
                   description = 'Фиксированная надбавка за смену в пятницу и субботу '
                       || 'при минимальной длительности',
                   widget_type = 'weekday_premium',
                   unit = '₽'
             where key = 'payroll.weekday_premium'
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            update app_setting
               set value = jsonb_build_object(
                       'friday',
                       coalesce(value -> 'amount', '200'::jsonb),
                       'saturday',
                       coalesce(value -> 'amount', '200'::jsonb)
                   ),
                   widget_options = coalesce(widget_options, '{}'::jsonb) - 'threshold_hours',
                   display_name = 'Надбавка за смену по дням недели',
                   description = 'Дополнительные суммы к ставке за смену в выбранные дни недели',
                   widget_type = 'weekday_premium',
                   unit = '₽'
             where key = 'payroll.weekday_premium'
            """
        )
    )

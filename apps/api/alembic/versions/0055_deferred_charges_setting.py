"""add deferred charges feature flag setting

Revision ID: 0055_deferred_charges_setting
Revises: 0055_deferred_charge_recipients
Create Date: 2026-06-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0055_deferred_charges_setting"
down_revision = "0055_deferred_charge_recipients"
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
                '6f3a7c8b-8c42-4c75-8f96-4b8c8b07e3a1',
                'payroll.deferred_charges_enabled',
                'false'::jsonb,
                'boolean',
                'payroll',
                'Распределение штрафов ревизии по нескольким ЗП',
                'Если включено, на странице Ревизии появляется подвкладка «Распределённые» и пункт меню «Распределить штраф…» на исключённой позиции.',
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
    op.execute(
        "delete from app_setting where key = 'payroll.deferred_charges_enabled'"
    )

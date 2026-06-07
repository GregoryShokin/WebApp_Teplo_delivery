"""seed payroll ndfl setting

Revision ID: 0076_payroll_ndfl_setting
Revises: 0075_payroll_rate_active_amount
Create Date: 2026-06-07
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0076_payroll_ndfl_setting"
down_revision = "0075_payroll_rate_active_amount"
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
                'ec4ad963-3534-4ee4-98bf-d8f9cfbbef57',
                'payroll.ndfl',
                '{
                    "enabled": false,
                    "rate": "0.13",
                    "default_employee_enabled": false,
                    "employee_ids": [],
                    "categories": [],
                    "roles": [],
                    "excluded_employee_ids": [],
                    "excluded_categories": [],
                    "excluded_roles": [],
                    "base": {
                        "base_pay": true,
                        "premium": true,
                        "percent_pay": true,
                        "vacation_pay": true,
                        "manual_bonuses": true,
                        "manual_penalties": false,
                        "seniority_allowance": true
                    }
                }'::jsonb,
                'object',
                'payroll',
                'НДФЛ в payroll',
                (
                    'Управляет расчётом удержанного НДФЛ: master switch, '
                    || 'scope сотрудников/категорий/ролей, ставка и база. '
                    || 'По умолчанию выключено до подтверждения бизнеса.'
                ),
                'json',
                null,
                null,
                now()
            )
            on conflict (key) do nothing
            """
        )
    )


def downgrade() -> None:
    op.execute("delete from app_setting where key = 'payroll.ndfl'")

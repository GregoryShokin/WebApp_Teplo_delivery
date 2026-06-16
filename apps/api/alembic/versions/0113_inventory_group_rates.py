"""inventory group rates for common/admins (full 100%)

Seeds editable ``inventory.rate_common`` / ``inventory.rate_admins`` settings
(default 1.00 = 100%) so the «Общая» and «Бар админов» groups charge the full
shortage of their цех (split between цеха for «Общая»), decoupled from the chefs
``inventory.rate_tier_50pct`` tier.

Revision ID: 0113_inventory_group_rates
Revises: 0112_deferred_charge_start
Create Date: 2026-06-16
"""

from __future__ import annotations

from alembic import op

revision = "0113_inventory_group_rates"
down_revision = "0112_deferred_charge_start"
branch_labels = None
depends_on = None

_SETTINGS = (
    (
        "b3f1c2a4-1d5e-4a6b-9c7d-1e2f3a4b5c6d",
        "inventory.rate_common",
        "Ставка «Общая»",
        "Процент штрафа для группы «Общая» — вся недостача цеха (делится между кухней и баром).",
    ),
    (
        "c4a2d3b5-2e6f-4b7c-8d9e-2f3a4b5c6d7e",
        "inventory.rate_admins",
        "Ставка «Бар админов»",
        "Процент штрафа для группы «Бар админов» — вся недостача цеха распределяется по кассирам.",
    ),
)


def upgrade() -> None:
    for setting_id, key, display_name, description in _SETTINGS:
        op.execute(
            f"""
            INSERT INTO app_setting (
                id, key, value, value_type, category, display_name,
                description, widget_type, widget_options, unit, updated_at
            )
            SELECT '{setting_id}', '{key}', '1.00'::jsonb, 'number', 'inventory',
                   '{display_name}', '{description}', 'percent', null, null, now()
            WHERE NOT EXISTS (SELECT 1 FROM app_setting WHERE key = '{key}')
            """
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM app_setting WHERE key IN ('inventory.rate_common', 'inventory.rate_admins')"
    )

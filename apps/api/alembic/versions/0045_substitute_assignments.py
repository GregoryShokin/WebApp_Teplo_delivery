"""add substitute assignments

Revision ID: 0045_substitute_assignments
Revises: 0043_fix_audit_penalty_work_date
Create Date: 2026-06-02
"""

from __future__ import annotations

import json
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0045_substitute_assignments"
down_revision = "0043_fix_audit_penalty_work_date"
branch_labels = None
depends_on = None


SUBSTITUTE_PAIRS_KEY = "payroll.substitute_pairs"


def upgrade() -> None:
    op.add_column(
        "employee_role_assignment",
        sa.Column(
            "is_substitute",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_employee_role_assignment_substitute",
        "employee_role_assignment",
        ["employee_id"],
        postgresql_where=sa.text("is_substitute = true"),
    )

    op.add_column(
        "employee",
        sa.Column(
            "requires_role_review",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "employee",
        sa.Column(
            "role_review_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    bind = op.get_bind()
    setting_id = uuid.uuid4()
    bind.execute(
        sa.text(
            """
            INSERT INTO app_setting (
                id, key, value, value_type, category, display_name,
                description, widget_type, widget_options, unit, updated_at
            )
            VALUES (
                :id, :key, CAST(:value AS jsonb), 'array', 'payroll',
                'Подменные роли',
                'Какие должности могут выходить на смены поваров и кассиров.',
                'json', NULL, NULL, now()
            )
            ON CONFLICT (key) DO NOTHING
            """
        ),
        {
            "id": setting_id,
            "key": SUBSTITUTE_PAIRS_KEY,
            "value": json.dumps(
                [
                    {
                        "from_position": "Управляющий",
                        "to_position": "Повар",
                        "add_to_schedule": False,
                    },
                    {
                        "from_position": "Менеджер",
                        "to_position": "Кассир",
                        "add_to_schedule": False,
                    },
                ],
                ensure_ascii=False,
            ),
        },
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            DELETE FROM app_setting_history
            WHERE setting_id IN (SELECT id FROM app_setting WHERE key = :key)
            """
        ),
        {"key": SUBSTITUTE_PAIRS_KEY},
    )
    bind.execute(sa.text("DELETE FROM app_setting WHERE key = :key"), {"key": SUBSTITUTE_PAIRS_KEY})
    op.drop_column("employee", "role_review_payload")
    op.drop_column("employee", "requires_role_review")
    op.drop_index(
        "ix_employee_role_assignment_substitute",
        table_name="employee_role_assignment",
    )
    op.drop_column("employee_role_assignment", "is_substitute")

"""extend employee dismissal reason settings

Revision ID: 0023_employee_dismissal_reasons
Revises: 0022_employee_change_events
Create Date: 2026-05-29
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0023_employee_dismissal_reasons"
down_revision = "0022_employee_change_events"
branch_labels = None
depends_on = None

DISMISSAL_REASONS = (
    ("voluntary", "По собственному желанию", False, 10),
    ("no_show", "Не вышел на смену", False, 20),
    ("discipline", "Нарушение дисциплины", False, 30),
    ("failed_trial", "Не прошёл стажировку", False, 40),
    ("layoff_no_shifts", "Сокращение/нет смен", False, 50),
    ("transfer", "Перевод", False, 60),
    ("other", "Другое", True, 70),
)


def upgrade() -> None:
    op.add_column(
        "employee_dismissal_reason",
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.drop_index("ix_employee_dismissal_reason_active", table_name="employee_dismissal_reason")
    op.create_index(
        "ix_employee_dismissal_reason_active",
        "employee_dismissal_reason",
        ["is_active", "sort_order", "label"],
    )
    _upsert_dismissal_reasons()

    op.add_column(
        "employee_change_event",
        sa.Column("reason_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_employee_change_event_reason_id_employee_dismissal_reason",
        "employee_change_event",
        "employee_dismissal_reason",
        ["reason_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_employee_change_event_reason_id_employee_dismissal_reason",
        "employee_change_event",
        type_="foreignkey",
    )
    op.drop_column("employee_change_event", "reason_id")

    op.drop_index("ix_employee_dismissal_reason_active", table_name="employee_dismissal_reason")
    op.create_index(
        "ix_employee_dismissal_reason_active",
        "employee_dismissal_reason",
        ["is_active", "label"],
    )
    op.drop_column("employee_dismissal_reason", "sort_order")


def _upsert_dismissal_reasons() -> None:
    conn = op.get_bind()
    for code, label, requires_comment, sort_order in DISMISSAL_REASONS:
        conn.execute(
            sa.text(
                """
                insert into employee_dismissal_reason (
                    id,
                    code,
                    label,
                    requires_comment,
                    is_system,
                    is_active,
                    sort_order
                )
                values (
                    :id,
                    :code,
                    :label,
                    :requires_comment,
                    true,
                    true,
                    :sort_order
                )
                on conflict (code) do update
                   set requires_comment = excluded.requires_comment,
                       sort_order = excluded.sort_order,
                       updated_at = now()
                """
            ),
            {
                "id": uuid.uuid4(),
                "code": code,
                "label": label,
                "requires_comment": requires_comment,
                "sort_order": sort_order,
            },
        )

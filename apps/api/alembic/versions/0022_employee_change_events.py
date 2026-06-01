"""add employee change events

Revision ID: 0022_employee_change_events
Revises: 0021_courier_deliveries
Create Date: 2026-05-29
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0022_employee_change_events"
down_revision = "0021_courier_deliveries"
branch_labels = None
depends_on = None

DISMISSAL_REASONS = (
    ("voluntary", "По собственному желанию", False),
    ("no_show", "Не вышел на смену", False),
    ("discipline", "Нарушение дисциплины", False),
    ("failed_trial", "Не прошёл стажировку", False),
    ("layoff_no_shifts", "Сокращение/нет смен", False),
    ("transfer", "Перевод", False),
    ("other", "Другое", True),
)


def upgrade() -> None:
    op.create_table(
        "employee_dismissal_reason",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=160), nullable=False),
        sa.Column(
            "requires_comment",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_employee_dismissal_reason"),
        sa.UniqueConstraint("code", name="uq_employee_dismissal_reason_code"),
    )
    op.create_index(
        "ix_employee_dismissal_reason_active",
        "employee_dismissal_reason",
        ["is_active", "label"],
    )
    _seed_dismissal_reasons()

    op.create_table(
        "employee_change_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("change_type", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_label", sa.String(length=160), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("before_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("diff", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("reason_label", sa.String(length=160), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("related_agent_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("related_agent_action_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("related_entity_type", sa.String(length=128), nullable=True),
        sa.Column("related_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "payroll_impact",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "payroll_impact_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "source in ('app', 'iiko_sync', 'system_migration')",
            name="ck_employee_change_event_source_value",
        ),
        sa.CheckConstraint(
            "status in ('success', 'error', 'requires_review', 'skipped')",
            name="ck_employee_change_event_status_value",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["user.id"],
            name="fk_employee_change_event_actor_user_id_user",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
            name="fk_employee_change_event_employee_id_employee",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["related_agent_action_id"],
            ["agent_action.id"],
            name="fk_employee_change_event_related_agent_action_id_agent_action",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["related_agent_run_id"],
            ["agent_run.id"],
            name="fk_employee_change_event_related_agent_run_id_agent_run",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_employee_change_event"),
    )
    op.create_index(
        "ix_employee_change_event_actor_user", "employee_change_event", ["actor_user_id"]
    )
    op.create_index("ix_employee_change_event_changed_at", "employee_change_event", ["changed_at"])
    op.create_index(
        "ix_employee_change_event_effective_from",
        "employee_change_event",
        ["effective_from"],
    )
    op.create_index(
        "ix_employee_change_event_employee_changed",
        "employee_change_event",
        ["employee_id", "changed_at"],
    )
    op.create_index(
        "ix_employee_change_event_source_status",
        "employee_change_event",
        ["source", "status"],
    )
    op.create_index("ix_employee_change_event_type", "employee_change_event", ["change_type"])


def downgrade() -> None:
    op.drop_index("ix_employee_change_event_type", table_name="employee_change_event")
    op.drop_index("ix_employee_change_event_source_status", table_name="employee_change_event")
    op.drop_index("ix_employee_change_event_employee_changed", table_name="employee_change_event")
    op.drop_index("ix_employee_change_event_effective_from", table_name="employee_change_event")
    op.drop_index("ix_employee_change_event_changed_at", table_name="employee_change_event")
    op.drop_index("ix_employee_change_event_actor_user", table_name="employee_change_event")
    op.drop_table("employee_change_event")
    op.drop_index(
        "ix_employee_dismissal_reason_active",
        table_name="employee_dismissal_reason",
    )
    op.drop_table("employee_dismissal_reason")


def _seed_dismissal_reasons() -> None:
    reason_table = sa.table(
        "employee_dismissal_reason",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("code", sa.String()),
        sa.column("label", sa.String()),
        sa.column("requires_comment", sa.Boolean()),
        sa.column("is_system", sa.Boolean()),
        sa.column("is_active", sa.Boolean()),
    )
    op.bulk_insert(
        reason_table,
        [
            {
                "id": uuid.uuid4(),
                "code": code,
                "label": label,
                "requires_comment": requires_comment,
                "is_system": True,
                "is_active": True,
            }
            for code, label, requires_comment in DISMISSAL_REASONS
        ],
    )

"""add effective dated employee position and allowance events

Revision ID: 0024_employee_effective_events
Revises: 0023_employee_dismissal_reasons
Create Date: 2026-05-30
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0024_employee_effective_events"
down_revision = "0023_employee_dismissal_reasons"
branch_labels = None
depends_on = None

POSITION_VALUES = (
    "Кассир",
    "Повар",
    "Управляющий",
    "Системный администратор",
    "Курьер",
    "Менеджер",
)
ALLOWANCE_TYPES = ("senior", "deputy_senior")


def upgrade() -> None:
    op.create_table(
        "employee_position_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.String(length=160), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            f"position in {_sql_values(POSITION_VALUES)}",
            name="ck_employee_position_event_position_canonical",
        ),
        sa.CheckConstraint(
            "effective_to is null or effective_to > effective_from",
            name="ck_employee_position_event_effective_range",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
            name="fk_employee_position_event_employee_id_employee",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "employee_id",
            "effective_from",
            name="uq_employee_position_event_employee_effective_from",
        ),
    )
    op.create_index(
        "ix_employee_position_event_employee_active",
        "employee_position_event",
        ["employee_id", "effective_from", "effective_to"],
    )

    op.create_table(
        "employee_allowance_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("allowance_type", sa.String(length=32), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            f"allowance_type in {_sql_values(ALLOWANCE_TYPES)}",
            name="ck_employee_allowance_event_type_value",
        ),
        sa.CheckConstraint(
            "effective_to is null or effective_to > effective_from",
            name="ck_employee_allowance_event_effective_range",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
            name="fk_employee_allowance_event_employee_id_employee",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "employee_id",
            "allowance_type",
            "effective_from",
            name="uq_employee_allowance_event_employee_type_effective_from",
        ),
    )
    op.create_index(
        "ix_employee_allowance_event_employee_active",
        "employee_allowance_event",
        ["employee_id", "allowance_type", "effective_from", "effective_to"],
    )

    op.create_table(
        "employee_pending_iiko_action",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("effective_on", sa.Date(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("related_entity_type", sa.String(length=128), nullable=True),
        sa.Column("related_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "action_type in ('update_position')",
            name="ck_employee_pending_iiko_action_type_value",
        ),
        sa.CheckConstraint(
            "status in ('pending', 'applied', 'failed', 'cancelled')",
            name="ck_employee_pending_iiko_action_status_value",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
            name="fk_employee_pending_iiko_action_employee_id_employee",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_employee_pending_iiko_action_due",
        "employee_pending_iiko_action",
        ["status", "effective_on"],
    )
    op.create_index(
        "ix_employee_pending_iiko_action_employee",
        "employee_pending_iiko_action",
        ["employee_id", "status"],
    )

    _backfill_employee_events()


def downgrade() -> None:
    op.drop_index(
        "ix_employee_pending_iiko_action_employee",
        table_name="employee_pending_iiko_action",
    )
    op.drop_index(
        "ix_employee_pending_iiko_action_due",
        table_name="employee_pending_iiko_action",
    )
    op.drop_table("employee_pending_iiko_action")

    op.drop_index(
        "ix_employee_allowance_event_employee_active",
        table_name="employee_allowance_event",
    )
    op.drop_table("employee_allowance_event")

    op.drop_index(
        "ix_employee_position_event_employee_active",
        table_name="employee_position_event",
    )
    op.drop_table("employee_position_event")


def _backfill_employee_events() -> None:
    conn = op.get_bind()
    employees = conn.execute(
        sa.text(
            """
            select id,
                   position,
                   coalesce(hire_date, current_date) as effective_from,
                   is_senior,
                   is_deputy_senior
              from employee
             where position is not null
            """
        )
    ).mappings()

    position_rows = []
    allowance_rows = []
    for row in employees:
        position_rows.append(
            {
                "id": uuid.uuid4(),
                "employee_id": row["id"],
                "position": row["position"],
                "effective_from": row["effective_from"],
                "effective_to": None,
                "comment": "Initial history backfill",
            }
        )
        if row["is_senior"]:
            allowance_rows.append(_allowance_backfill(row, "senior"))
        if row["is_deputy_senior"]:
            allowance_rows.append(_allowance_backfill(row, "deputy_senior"))

    if position_rows:
        op.bulk_insert(
            sa.table(
                "employee_position_event",
                sa.column("id", postgresql.UUID(as_uuid=True)),
                sa.column("employee_id", postgresql.UUID(as_uuid=True)),
                sa.column("position", sa.String()),
                sa.column("effective_from", sa.Date()),
                sa.column("effective_to", sa.Date()),
                sa.column("comment", sa.Text()),
            ),
            position_rows,
        )
    if allowance_rows:
        op.bulk_insert(
            sa.table(
                "employee_allowance_event",
                sa.column("id", postgresql.UUID(as_uuid=True)),
                sa.column("employee_id", postgresql.UUID(as_uuid=True)),
                sa.column("allowance_type", sa.String()),
                sa.column("is_enabled", sa.Boolean()),
                sa.column("effective_from", sa.Date()),
                sa.column("effective_to", sa.Date()),
                sa.column("comment", sa.Text()),
            ),
            allowance_rows,
        )


def _allowance_backfill(row: sa.RowMapping, allowance_type: str) -> dict[str, object]:
    return {
        "id": uuid.uuid4(),
        "employee_id": row["id"],
        "allowance_type": allowance_type,
        "is_enabled": True,
        "effective_from": row["effective_from"],
        "effective_to": None,
        "comment": "Initial history backfill",
    }


def _sql_values(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"

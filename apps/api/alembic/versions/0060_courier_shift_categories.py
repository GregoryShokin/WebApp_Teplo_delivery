"""move courier categories to schedule shifts

Revision ID: 0060_courier_shift_categories
Revises: 0059_prep_intern_availability
Create Date: 2026-06-05
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0060_courier_shift_categories"
down_revision = "0059_prep_intern_availability"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

NEW_MATCH_STATUSES = (
    "matched_primary",
    "short_primary",
    "no_show_primary",
    "matched_secondary",
    "short_secondary",
    "no_show_secondary",
    "helping",
    "not_counted",
)

OLD_MATCH_STATUSES = ("matched", "no_show", "helping", "short_shift")

SCHEDULE_SETTINGS = [
    {
        "id": "313b2d68-c721-4817-aa69-6c78328f757b",
        "key": "couriers.schedule.default_start_time",
        "value": '"10:00"',
        "display_name": "Начало смены курьера по умолчанию",
        "description": (
            "Время начала, которое подставляется при создании смены курьера без ручного времени."
        ),
    },
    {
        "id": "90a64d86-5284-44f8-b808-c9cfb2104f2e",
        "key": "couriers.schedule.default_end_time",
        "value": '"22:00"',
        "display_name": "Конец смены курьера по умолчанию",
        "description": (
            "Время окончания, которое подставляется при создании смены курьера без ручного времени."
        ),
    },
]


def upgrade() -> None:
    conn = op.get_bind()
    assignment_count = conn.execute(
        sa.text("select count(*) from courier_category_assignment")
    ).scalar_one()
    if assignment_count:
        log.warning(
            "courier_category_assignment contains %s rows; dropping by product decision",
            assignment_count,
        )
    op.drop_table("courier_category_assignment")
    postgresql.ENUM(name="courier_category").drop(conn, checkfirst=True)

    op.add_column(
        "courier_schedule_entry",
        sa.Column(
            "category",
            sa.String(length=16),
            server_default="secondary",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_courier_schedule_entry_category"),
        "courier_schedule_entry",
        "category in ('primary', 'secondary')",
    )
    op.create_index(
        "ix_courier_schedule_entry_work_date_category",
        "courier_schedule_entry",
        ["work_date", "category"],
    )
    _upgrade_match_status_enum()
    _seed_schedule_settings()


def downgrade() -> None:
    _delete_schedule_settings()
    _downgrade_match_status_enum()
    op.drop_index(
        "ix_courier_schedule_entry_work_date_category",
        table_name="courier_schedule_entry",
    )
    op.drop_constraint(
        op.f("ck_courier_schedule_entry_category"),
        "courier_schedule_entry",
        type_="check",
    )
    op.drop_column("courier_schedule_entry", "category")
    _restore_category_assignment_table()


def _replace_match_status_enum(
    *,
    temp_enum_name: str,
    temp_column_name: str,
    temp_values: tuple[str, ...],
    populate_sql: tuple[str, ...],
) -> None:
    conn = op.get_bind()
    temp_enum = postgresql.ENUM(
        *temp_values,
        name=temp_enum_name,
        create_type=False,
    )
    temp_enum.create(conn, checkfirst=True)

    op.add_column(
        "courier_shift_match",
        sa.Column(temp_column_name, temp_enum, nullable=True),
    )

    for statement in populate_sql:
        op.execute(statement)
    op.drop_column("courier_shift_match", "status")
    op.execute("DROP TYPE courier_shift_match_status")
    op.execute(f"ALTER TYPE {temp_enum_name} RENAME TO courier_shift_match_status")
    op.alter_column(
        "courier_shift_match",
        temp_column_name,
        new_column_name="status",
    )
    op.alter_column(
        "courier_shift_match",
        "status",
        nullable=False,
    )


def _upgrade_match_status_enum() -> None:
    _replace_match_status_enum(
        temp_enum_name="courier_shift_match_status_new",
        temp_column_name="status_new",
        temp_values=NEW_MATCH_STATUSES,
        populate_sql=(
            """
                UPDATE courier_shift_match m
                SET status_new = (
                    CASE
                        WHEN m.status::text = 'matched' AND s.category = 'primary'
                            THEN 'matched_primary'::courier_shift_match_status_new
                        WHEN m.status::text = 'matched'
                            THEN 'matched_secondary'::courier_shift_match_status_new
                        WHEN m.status::text = 'no_show' AND s.category = 'primary'
                            THEN 'no_show_primary'::courier_shift_match_status_new
                        WHEN m.status::text = 'no_show'
                            THEN 'no_show_secondary'::courier_shift_match_status_new
                        WHEN m.status::text = 'short_shift' AND s.category = 'primary'
                            THEN 'short_primary'::courier_shift_match_status_new
                        WHEN m.status::text = 'short_shift'
                            THEN 'short_secondary'::courier_shift_match_status_new
                        WHEN m.status::text = 'helping'
                            THEN 'helping'::courier_shift_match_status_new
                        ELSE 'not_counted'::courier_shift_match_status_new
                    END
                )
                FROM courier_schedule_entry s
                WHERE s.id = m.schedule_entry_id
            """,
            """
                UPDATE courier_shift_match
                SET status_new = (
                    CASE
                        WHEN status::text = 'matched' AND schedule_entry_id IS NULL
                            THEN 'not_counted'::courier_shift_match_status_new
                        WHEN status::text = 'matched'
                            THEN 'matched_secondary'::courier_shift_match_status_new
                        WHEN status::text = 'no_show'
                            THEN 'no_show_secondary'::courier_shift_match_status_new
                        WHEN status::text = 'short_shift'
                            THEN 'short_secondary'::courier_shift_match_status_new
                        WHEN status::text = 'helping'
                            THEN 'helping'::courier_shift_match_status_new
                        ELSE 'not_counted'::courier_shift_match_status_new
                    END
                )
                WHERE status_new IS NULL
            """,
        ),
    )


def _downgrade_match_status_enum() -> None:
    _replace_match_status_enum(
        temp_enum_name="courier_shift_match_status_old",
        temp_column_name="status_old",
        temp_values=OLD_MATCH_STATUSES,
        populate_sql=(
            """
                UPDATE courier_shift_match
                SET status_old = (
                    CASE
                        WHEN status::text IN (
                            'matched_primary',
                            'matched_secondary',
                            'not_counted'
                        ) THEN 'matched'::courier_shift_match_status_old
                        WHEN status::text IN ('no_show_primary', 'no_show_secondary')
                            THEN 'no_show'::courier_shift_match_status_old
                        WHEN status::text IN ('short_primary', 'short_secondary')
                            THEN 'short_shift'::courier_shift_match_status_old
                        WHEN status::text = 'helping'
                            THEN 'helping'::courier_shift_match_status_old
                        ELSE 'matched'::courier_shift_match_status_old
                    END
                )
            """,
        ),
    )


def _seed_schedule_settings() -> None:
    for setting in SCHEDULE_SETTINGS:
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
                    (:id)::uuid,
                    :key,
                    (:value)::jsonb,
                    'string',
                    'couriers',
                    :display_name,
                    :description,
                    'time',
                    null,
                    null,
                    now()
                )
                on conflict (key) do nothing
                """
            ).bindparams(**setting)
        )


def _delete_schedule_settings() -> None:
    keys_sql = ", ".join(f"'{setting['key']}'" for setting in SCHEDULE_SETTINGS)
    op.execute(
        f"delete from app_setting_history where setting_id in "
        f"(select id from app_setting where key in ({keys_sql}))"
    )
    op.execute(f"delete from app_setting where key in ({keys_sql})")


def _restore_category_assignment_table() -> None:
    category_enum = postgresql.ENUM(
        "primary",
        "secondary",
        name="courier_category",
        create_type=False,
    )
    category_enum.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "courier_category_assignment",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category", category_enum, nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "effective_to is null or effective_to >= effective_from",
            name="ck_courier_category_assignment_effective_range",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
            name="fk_courier_category_assignment_employee_id_employee",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["employee.id"],
            name="fk_courier_category_assignment_created_by_employee",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_courier_category_assignment_employee_dates",
        "courier_category_assignment",
        ["employee_id", "effective_from", "effective_to"],
    )
    op.create_index(
        "uq_courier_category_assignment_one_open",
        "courier_category_assignment",
        ["employee_id"],
        unique=True,
        postgresql_where=sa.text("effective_to is null"),
    )

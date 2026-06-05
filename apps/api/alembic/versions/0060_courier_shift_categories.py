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
        "key": "couriers.schedule.default_start_time",
        "value": '"10:00"',
        "display_name": "Начало смены курьера по умолчанию",
        "description": (
            "Время начала, которое подставляется при создании смены курьера без ручного времени."
        ),
    },
    {
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
        "ck_courier_schedule_entry_category",
        "courier_schedule_entry",
        "category in ('primary', 'secondary')",
    )
    op.create_index(
        "ix_courier_schedule_entry_work_date_category",
        "courier_schedule_entry",
        ["work_date", "category"],
    )
    _replace_match_status_enum(OLD_MATCH_STATUSES, NEW_MATCH_STATUSES, _upgrade_status_case())
    _seed_schedule_settings()


def downgrade() -> None:
    _delete_schedule_settings()
    _replace_match_status_enum(NEW_MATCH_STATUSES, OLD_MATCH_STATUSES, _downgrade_status_case())
    op.drop_index(
        "ix_courier_schedule_entry_work_date_category",
        table_name="courier_schedule_entry",
    )
    op.drop_constraint(
        "ck_courier_schedule_entry_category",
        "courier_schedule_entry",
        type_="check",
    )
    op.drop_column("courier_schedule_entry", "category")
    _restore_category_assignment_table()


def _replace_match_status_enum(
    old_values: tuple[str, ...],
    new_values: tuple[str, ...],
    status_case_sql: str,
) -> None:
    conn = op.get_bind()
    temp_enum = postgresql.ENUM(
        *new_values,
        name="courier_shift_match_status_new",
        create_type=False,
    )
    temp_enum.create(conn, checkfirst=True)
    op.execute(
        sa.text(
            f"""
            alter table courier_shift_match
            alter column status type courier_shift_match_status_new
            using ({status_case_sql})::courier_shift_match_status_new
            """
        )
    )
    postgresql.ENUM(*old_values, name="courier_shift_match_status").drop(
        conn,
        checkfirst=True,
    )
    op.execute("alter type courier_shift_match_status_new rename to courier_shift_match_status")


def _upgrade_status_case() -> str:
    return """
        case
            when status::text = 'matched' and schedule_entry_id is null then 'not_counted'
            when status::text = 'matched'
                and exists (
                    select 1
                    from courier_schedule_entry schedule
                    where schedule.id = courier_shift_match.schedule_entry_id
                      and schedule.category = 'primary'
                )
                then 'matched_primary'
            when status::text = 'matched' then 'matched_secondary'
            when status::text = 'no_show'
                and exists (
                    select 1
                    from courier_schedule_entry schedule
                    where schedule.id = courier_shift_match.schedule_entry_id
                      and schedule.category = 'primary'
                )
                then 'no_show_primary'
            when status::text = 'no_show' then 'no_show_secondary'
            when status::text = 'short_shift'
                and exists (
                    select 1
                    from courier_schedule_entry schedule
                    where schedule.id = courier_shift_match.schedule_entry_id
                      and schedule.category = 'primary'
                )
                then 'short_primary'
            when status::text = 'short_shift' then 'short_secondary'
            when status::text = 'helping' then 'helping'
            else 'not_counted'
        end
    """


def _downgrade_status_case() -> str:
    return """
        case
            when status::text in (
                'matched_primary',
                'matched_secondary',
                'not_counted'
            ) then 'matched'
            when status::text in ('no_show_primary', 'no_show_secondary') then 'no_show'
            when status::text in ('short_primary', 'short_secondary') then 'short_shift'
            when status::text = 'helping' then 'helping'
            else 'matched'
        end
    """


def _seed_schedule_settings() -> None:
    for setting in SCHEDULE_SETTINGS:
        op.execute(
            sa.text(
                """
                insert into app_setting (
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

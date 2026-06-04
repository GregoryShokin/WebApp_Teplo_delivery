"""add courier backend foundation

Revision ID: 0056_courier_backend
Revises: 0055_deferred_charges_setting
Create Date: 2026-06-04
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0056_courier_backend"
down_revision = "0055_deferred_charges_setting"
branch_labels = None
depends_on = None


CRITERIA = [
    {"id": 1, "code": "praise_from_client", "label": "Похвала от клиента", "score": 7},
    {"id": 2, "code": "fast", "label": "Быстрый", "score": 5},
    {"id": 3, "code": "responsible", "label": "Ответственный", "score": 3},
    {"id": 4, "code": "pleasant_work", "label": "Приятно работать", "score": 1},
    {"id": 5, "code": "conflict", "label": "Конфликт", "score": 0},
    {"id": 6, "code": "no_distinctions", "label": "Без отличий", "score": 0},
    {"id": 7, "code": "missed_comments", "label": "Не видет комменты", "score": -1},
    {"id": 8, "code": "no_change_taken", "label": "Не взял сдачу", "score": -1},
    {"id": 9, "code": "slow_response", "label": "Долгий отклик", "score": -2},
    {"id": 10, "code": "broke_down", "label": "Сломался", "score": -2},
    {"id": 11, "code": "unreachable", "label": "Не выходит на связь", "score": -3},
    {"id": 12, "code": "no_route", "label": "Отсутствие маршрута", "score": -3},
    {"id": 13, "code": "slow", "label": "Медленный", "score": -5},
    {"id": 14, "code": "overturned_order", "label": "Перевернул заказ", "score": -7},
]

SETTINGS = [
    {
        "id": "59ee730f-63df-477b-ae0c-332fb4a17d7d",
        "key": "couriers.deposit.target_amount",
        "value": 5000,
        "value_type": "number",
        "display_name": "Целевой депозит курьера",
        "description": "Общая целевая сумма депозита для курьеров.",
        "widget_type": "number",
        "unit": "₽",
    },
    {
        "id": "2bd9ec50-6879-4eaa-9480-1d3396889261",
        "key": "couriers.deposit.withhold_primary",
        "value": 500,
        "value_type": "number",
        "display_name": "Удержание primary",
        "description": "Сумма ручного удержания за смену основного курьера.",
        "widget_type": "number",
        "unit": "₽",
    },
    {
        "id": "425cdba8-f5aa-4ffa-b950-bb4c300d4f62",
        "key": "couriers.deposit.withhold_secondary",
        "value": 200,
        "value_type": "number",
        "display_name": "Удержание secondary",
        "description": "Сумма ручного удержания за смену второстепенного курьера.",
        "widget_type": "number",
        "unit": "₽",
    },
    {
        "id": "938797db-f755-4231-9e15-36a889c99f06",
        "key": "couriers.deposit.auto_withhold_enabled",
        "value": False,
        "value_type": "boolean",
        "display_name": "Автоудержание депозита",
        "description": "Флаг будущего автоматического удержания через iiko.",
        "widget_type": "boolean",
        "unit": None,
    },
    {
        "id": "7259a9b4-4b6e-4f3d-adcc-f45da5c2ed02",
        "key": "couriers.evaluation.edit_window_minutes",
        "value": 30,
        "value_type": "number",
        "display_name": "Окно редактирования оценок",
        "description": "Сколько минут автор может менять или удалять свою оценку.",
        "widget_type": "number",
        "unit": "мин",
    },
]


def upgrade() -> None:
    category_enum = postgresql.ENUM(
        "primary",
        "secondary",
        name="courier_category",
        create_type=False,
    )
    transaction_enum = postgresql.ENUM(
        "top_up",
        "return",
        "forfeit",
        name="courier_deposit_transaction_type",
        create_type=False,
    )
    source_enum = postgresql.ENUM(
        "web",
        "telegram",
        "api",
        name="courier_evaluation_source",
        create_type=False,
    )
    category_enum.create(op.get_bind(), checkfirst=True)
    transaction_enum.create(op.get_bind(), checkfirst=True)
    source_enum.create(op.get_bind(), checkfirst=True)

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

    op.create_table(
        "courier_deposit_account",
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "target_amount_cents",
            sa.BigInteger(),
            server_default="500000",
            nullable=False,
        ),
        sa.Column(
            "opening_balance_cents",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "opening_date",
            sa.Date(),
            server_default=sa.text("current_date"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
            name="fk_courier_deposit_account_employee_id_employee",
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "courier_deposit_transaction",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("account_employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transaction_type", transaction_enum, nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "amount_cents > 0",
            name="ck_courier_deposit_transaction_amount_positive",
        ),
        sa.ForeignKeyConstraint(
            ["account_employee_id"],
            ["courier_deposit_account.employee_id"],
            name="fk_courier_deposit_transaction_account_employee_id_account",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["employee.id"],
            name="fk_courier_deposit_transaction_created_by_employee",
            ondelete="RESTRICT",
        ),
    )

    op.create_table(
        "courier_evaluation_criterion",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("code", sa.String(length=80), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "score between -7 and 7",
            name="ck_courier_evaluation_criterion_score_range",
        ),
        sa.UniqueConstraint("code", name="uq_courier_evaluation_criterion_code"),
    )
    criteria_table = sa.table(
        "courier_evaluation_criterion",
        sa.column("id", sa.Integer()),
        sa.column("code", sa.String()),
        sa.column("label", sa.String()),
        sa.column("score", sa.Integer()),
        sa.column("is_active", sa.Boolean()),
        sa.column("display_order", sa.Integer()),
    )
    op.bulk_insert(
        criteria_table,
        [
            criterion | {"is_active": True, "display_order": criterion["id"]}
            for criterion in CRITERIA
        ],
    )
    op.execute(
        "select setval(pg_get_serial_sequence('courier_evaluation_criterion', 'id'), 14, true)"
    )

    op.create_table(
        "courier_evaluation",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("courier_employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("criterion_id", sa.Integer(), nullable=False),
        sa.Column("score_snapshot", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "evaluated_at",
            sa.Date(),
            server_default=sa.text("current_date"),
            nullable=False,
        ),
        sa.Column("source", source_enum, server_default="web", nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "score_snapshot between -7 and 7",
            name="ck_courier_evaluation_score_snapshot_range",
        ),
        sa.ForeignKeyConstraint(
            ["courier_employee_id"],
            ["employee.id"],
            name="fk_courier_evaluation_courier_employee_id_employee",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["criterion_id"],
            ["courier_evaluation_criterion.id"],
            name="fk_courier_evaluation_criterion_id_criterion",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["employee.id"],
            name="fk_courier_evaluation_created_by_employee",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_courier_evaluation_courier_date",
        "courier_evaluation",
        ["courier_employee_id", "evaluated_at"],
    )
    op.create_index(
        "ix_courier_evaluation_author_created",
        "courier_evaluation",
        ["created_by", "created_at"],
    )

    op.create_table(
        "courier_schedule_entry",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("courier_employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("planned_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "planned_end_at > planned_start_at",
            name="ck_courier_schedule_entry_time_range",
        ),
        sa.ForeignKeyConstraint(
            ["courier_employee_id"],
            ["employee.id"],
            name="fk_courier_schedule_entry_courier_employee_id_employee",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["employee.id"],
            name="fk_courier_schedule_entry_created_by_employee",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "courier_employee_id",
            "work_date",
            name="uq_courier_schedule_entry_courier_work_date",
        ),
    )

    _seed_settings()


def downgrade() -> None:
    ids_sql = ", ".join(f"'{setting_id}'" for setting_id in _setting_ids())
    op.execute(f"delete from app_setting_history where setting_id in ({ids_sql})")
    keys_sql = ", ".join(f"'{setting['key']}'" for setting in SETTINGS)
    op.execute(f"delete from app_setting where key in ({keys_sql})")

    op.drop_table("courier_schedule_entry")
    op.drop_index("ix_courier_evaluation_author_created", table_name="courier_evaluation")
    op.drop_index("ix_courier_evaluation_courier_date", table_name="courier_evaluation")
    op.drop_table("courier_evaluation")
    op.drop_table("courier_evaluation_criterion")
    op.drop_table("courier_deposit_transaction")
    op.drop_table("courier_deposit_account")
    op.drop_index(
        "uq_courier_category_assignment_one_open",
        table_name="courier_category_assignment",
    )
    op.drop_index(
        "ix_courier_category_assignment_employee_dates",
        table_name="courier_category_assignment",
    )
    op.drop_table("courier_category_assignment")

    postgresql.ENUM(name="courier_evaluation_source").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="courier_deposit_transaction_type").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="courier_category").drop(op.get_bind(), checkfirst=True)


def _seed_settings() -> None:
    admin_user_id = op.get_bind().scalar(
        sa.text('select id from "user" order by created_at limit 1')
    )

    app_setting_table = sa.table(
        "app_setting",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("key", sa.String()),
        sa.column("value", postgresql.JSONB()),
        sa.column("value_type", sa.String()),
        sa.column("category", sa.String()),
        sa.column("display_name", sa.Text()),
        sa.column("description", sa.Text()),
        sa.column("widget_type", sa.Text()),
        sa.column("widget_options", postgresql.JSONB()),
        sa.column("unit", sa.Text()),
        sa.column("updated_by_user_id", postgresql.UUID(as_uuid=True)),
    )
    op.bulk_insert(
        app_setting_table,
        [
            {
                **setting,
                "id": uuid.UUID(str(setting["id"])),
                "category": "couriers",
                "widget_options": None,
                "updated_by_user_id": admin_user_id,
            }
            for setting in SETTINGS
        ],
    )

    app_setting_history_table = sa.table(
        "app_setting_history",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("setting_id", postgresql.UUID(as_uuid=True)),
        sa.column("old_value", postgresql.JSONB()),
        sa.column("new_value", postgresql.JSONB()),
        sa.column("changed_by_user_id", postgresql.UUID(as_uuid=True)),
    )
    op.bulk_insert(
        app_setting_history_table,
        [
            {
                "id": uuid.UUID(history_id),
                "setting_id": uuid.UUID(str(setting["id"])),
                "old_value": None,
                "new_value": setting["value"],
                "changed_by_user_id": admin_user_id,
            }
            for setting, history_id in zip(
                SETTINGS,
                (
                    "7f4cf372-8721-414a-b542-bbeeb0a9b58f",
                    "e18c7d89-bd69-4cc8-b7c4-b1a717da30af",
                    "5791b6eb-5e18-4660-b896-af1d0e296fa2",
                    "f2e78374-2aba-4894-807c-008bf970aff9",
                    "f55d477a-fcc2-4736-bf12-45e6f1f3a425",
                ),
                strict=True,
            )
        ],
    )


def _setting_ids() -> list[str]:
    return [setting["id"] for setting in SETTINGS]

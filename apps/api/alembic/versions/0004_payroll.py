"""payroll weekly runs, attendance, deposits and accumulation fund

Revision ID: 0004_payroll
Revises: 0003_employee_extension
Create Date: 2026-05-27
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004_payroll"
down_revision = "0003_employee_extension"
branch_labels = None
depends_on = None


PAYROLL_SETTING_KEYS = (
    "payroll.role_category_rates",
    "payroll.category_rules",
    "payroll.revenue_percent_tiers",
    "payroll.allowances",
    "payroll.fund_rates_by_tenure",
    "payroll.mock_daily_revenue",
    "payroll.deposit_auto_withholding_enabled",
    "payroll.deposit_write_offs",
)


def upgrade() -> None:
    op.create_table(
        "payroll_period",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("period_type", sa.String(length=16), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("payroll_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("period_type = 'week'", name="ck_payroll_period_type_week"),
        sa.ForeignKeyConstraint(
            ["finalized_by_user_id"],
            ["user.id"],
            name="fk_payroll_period_finalized_by_user_id_user",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "period_type",
            "start_date",
            "end_date",
            name="uq_payroll_period_type_start_end",
        ),
    )

    op.create_table(
        "attendance_entry",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("period_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("minutes_worked", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("station", sa.String(length=160), nullable=True),
        sa.Column("role", sa.String(length=160), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("quality_status", sa.String(length=64), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "source in ('iiko', 'manual', 'telegram')",
            name="ck_attendance_entry_source",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
            name="fk_attendance_entry_employee_id_employee",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["period_id"],
            ["payroll_period.id"],
            name="fk_attendance_entry_period_id_payroll_period",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_attendance_entry_period_employee",
        "attendance_entry",
        ["period_id", "employee_id"],
    )
    op.create_index("ix_attendance_entry_work_date", "attendance_entry", ["work_date"])

    op.create_table(
        "payroll_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("period_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "blocking_issues",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.ForeignKeyConstraint(
            ["period_id"],
            ["payroll_period.id"],
            name="fk_payroll_run_period_id_payroll_period",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_payroll_run_period_started", "payroll_run", ["period_id", "started_at"])

    op.create_table(
        "payroll_line",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=160), nullable=False),
        sa.Column("base_pay", sa.Numeric(14, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("premium", sa.Numeric(14, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("percent_pay", sa.Numeric(14, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("fund_accrual", sa.Numeric(14, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("deduction", sa.Numeric(14, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("total_payable", sa.Numeric(14, 2), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "components",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
            name="fk_payroll_line_employee_id_employee",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["payroll_run.id"],
            name="fk_payroll_line_run_id_payroll_run",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "run_id",
            "employee_id",
            "role",
            name="uq_payroll_line_run_employee_role",
        ),
    )
    op.create_index("ix_payroll_line_run", "payroll_line", ["run_id"])

    op.create_table(
        "deposit_account",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("balance", sa.Numeric(14, 2), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "last_updated",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
            name="fk_deposit_account_employee_id_employee",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("employee_id", name="uq_deposit_account_employee"),
    )

    op.create_table(
        "deposit_transaction",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("transaction_type", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "transaction_type in ('accrual', 'payout', 'write_off')",
            name="ck_deposit_transaction_type",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
            name="fk_deposit_transaction_employee_id_employee",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["payroll_run.id"],
            name="fk_deposit_transaction_run_id_payroll_run",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_deposit_transaction_employee_created",
        "deposit_transaction",
        ["employee_id", "created_at"],
    )

    op.create_table(
        "accumulation_fund_account",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column(
            "accumulated_amount",
            sa.Numeric(14, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "paid_out_amount",
            sa.Numeric(14, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
            name="fk_accumulation_fund_account_employee_id_employee",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("employee_id", "year", name="uq_accumulation_fund_employee_year"),
    )

    _seed_payroll_settings()


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "delete from app_setting_history "
            "where setting_id in (select id from app_setting where key = any(:keys))"
        ),
        {"keys": list(PAYROLL_SETTING_KEYS)},
    )
    conn.execute(
        sa.text("delete from app_setting where key = any(:keys)"),
        {"keys": list(PAYROLL_SETTING_KEYS)},
    )

    op.drop_table("accumulation_fund_account")
    op.drop_index("ix_deposit_transaction_employee_created", table_name="deposit_transaction")
    op.drop_table("deposit_transaction")
    op.drop_table("deposit_account")
    op.drop_index("ix_payroll_line_run", table_name="payroll_line")
    op.drop_table("payroll_line")
    op.drop_index("ix_payroll_run_period_started", table_name="payroll_run")
    op.drop_table("payroll_run")
    op.drop_index("ix_attendance_entry_work_date", table_name="attendance_entry")
    op.drop_index("ix_attendance_entry_period_employee", table_name="attendance_entry")
    op.drop_table("attendance_entry")
    op.drop_table("payroll_period")


def _seed_payroll_settings() -> None:
    conn = op.get_bind()
    existing = set(
        conn.execute(
            sa.text("select key from app_setting where key = any(:keys)"),
            {"keys": list(PAYROLL_SETTING_KEYS)},
        ).scalars()
    )
    admin_user_id = conn.scalar(sa.text('select id from "user" order by created_at limit 1'))

    settings = [
        {
            "id": uuid.uuid4(),
            "key": "payroll.role_category_rates",
            "value": {
                "Администратор": {"2": 2200, "3": 2000, "4": 1800},
                "Заготовщик": {"5": 2200},
                "Пиццерист": {"1": 2600, "2": 2200, "3": 2000, "4": 1800, "5": 2000},
                "Сушист": {"1": 2800, "2": 2400, "3": 2200, "4": 2000, "5": 2000, "6": 0},
                "Шаурмист": {"3": 2000, "4": 1800, "5": 1800},
            },
            "value_type": "object",
            "category": "Зарплата",
            "description": "Ставки полной 12-часовой смены по payroll-роли и категории.",
        },
        {
            "id": uuid.uuid4(),
            "key": "payroll.category_rules",
            "value": {
                "1": {"coeff": 10, "deposit_target": 20000, "deposit_withholding": 1000},
                "2": {"coeff": 7.5, "deposit_target": 15000, "deposit_withholding": 1000},
                "3": {"coeff": 5, "deposit_target": 10000, "deposit_withholding": 1000},
                "4": {"coeff": 0, "deposit_target": 7000, "deposit_withholding": 1000},
                "5": {"coeff": 0, "deposit_target": 7000, "deposit_withholding": 1000},
                "6": {"coeff": 0, "deposit_target": 0, "deposit_withholding": 0},
            },
            "value_type": "object",
            "category": "Зарплата",
            "description": "Коэффициенты процента, депозит и удержание по категории.",
        },
        {
            "id": uuid.uuid4(),
            "key": "payroll.revenue_percent_tiers",
            "value": [
                {"from": 50000, "rate": 0.035},
                {"from": 140000, "rate": 0.045},
                {"from": 190000, "rate": 0.055},
                {"from": 550000, "rate": 0.065},
            ],
            "value_type": "object",
            "category": "Зарплата",
            "description": "Пороговая таблица процента от дневной выручки.",
        },
        {
            "id": uuid.uuid4(),
            "key": "payroll.allowances",
            "value": {"senior": 500, "deputy_senior": 300},
            "value_type": "object",
            "category": "Зарплата",
            "description": "Надбавки к ставке смены.",
        },
        {
            "id": uuid.uuid4(),
            "key": "payroll.fund_rates_by_tenure",
            "value": [
                {"min_years": 0.5, "rate": 0.05},
                {"min_years": 1.0, "rate": 0.10},
                {"min_years": 1.5, "rate": 0.15},
            ],
            "value_type": "object",
            "category": "Зарплата",
            "description": "Процент накопительного фонда от оклада по стажу.",
        },
        {
            "id": uuid.uuid4(),
            "key": "payroll.mock_daily_revenue",
            "value": {},
            "value_type": "object",
            "category": "Зарплата",
            "description": "Временный источник дневной выручки до подключения iiko OLAP.",
        },
        {
            "id": uuid.uuid4(),
            "key": "payroll.deposit_auto_withholding_enabled",
            "value": False,
            "value_type": "boolean",
            "category": "Зарплата",
            "description": "Автоматически удерживать депозит по правилам категории.",
        },
        {
            "id": uuid.uuid4(),
            "key": "payroll.deposit_write_offs",
            "value": [],
            "value_type": "object",
            "category": "Зарплата",
            "description": "Ручные списания депозитов до появления manual payroll events.",
        },
    ]
    settings = [setting for setting in settings if setting["key"] not in existing]
    if not settings:
        return

    app_setting_table = sa.table(
        "app_setting",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("key", sa.String()),
        sa.column("value", postgresql.JSONB()),
        sa.column("value_type", sa.String()),
        sa.column("category", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("updated_by_user_id", postgresql.UUID(as_uuid=True)),
    )
    op.bulk_insert(
        app_setting_table,
        [setting | {"updated_by_user_id": admin_user_id} for setting in settings],
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
                "id": uuid.uuid4(),
                "setting_id": setting["id"],
                "old_value": None,
                "new_value": setting["value"],
                "changed_by_user_id": admin_user_id,
            }
            for setting in settings
        ],
    )

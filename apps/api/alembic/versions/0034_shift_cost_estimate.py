"""add shift cost estimate forecast runs

Revision ID: 0034_shift_cost_estimate
Revises: 0033_revenue_forecast
Create Date: 2026-06-01
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0034_shift_cost_estimate"
down_revision = "0033_revenue_forecast"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payroll_forecast_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("shift_schedule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "run_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("run_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("rule_version", sa.String(64), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("total_revenue_forecast", sa.Numeric(14, 2), nullable=True),
        sa.Column("total_shift_cost_estimate", sa.Numeric(14, 2), nullable=True),
        sa.Column("fot_to_revenue_pct", sa.Numeric(8, 5), nullable=True),
        sa.Column("shifts_total", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "shifts_with_warnings",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["shift_schedule_id"], ["shift_schedule.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "status IN ('draft','completed','superseded')",
            name="ck_payroll_forecast_run_status",
        ),
    )
    op.create_index(
        "ix_payroll_forecast_run_schedule_run_at",
        "payroll_forecast_run",
        ["shift_schedule_id", "run_at"],
    )

    op.create_table(
        "shift_cost_estimate",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("forecast_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scheduled_shift_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revenue_forecast_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("planned_hours", sa.Numeric(6, 2), nullable=False),
        sa.Column(
            "base_salary_estimate",
            sa.Numeric(14, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "weekday_premium_estimate",
            sa.Numeric(14, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "allowance_estimate",
            sa.Numeric(14, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "revenue_percent_estimate",
            sa.Numeric(14, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "fund_accrual_estimate",
            sa.Numeric(14, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "total_cost_estimate",
            sa.Numeric(14, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("quality_status", sa.String(24), nullable=False),
        sa.Column(
            "quality_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "breakdown",
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
        sa.ForeignKeyConstraint(
            ["forecast_run_id"],
            ["payroll_forecast_run.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["scheduled_shift_id"],
            ["scheduled_shift.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["revenue_forecast_id"],
            ["revenue_forecast.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "quality_status IN ('ok','requires_review')",
            name="ck_shift_cost_estimate_quality_status",
        ),
        sa.UniqueConstraint(
            "forecast_run_id",
            "scheduled_shift_id",
            name="uq_shift_cost_estimate_run_shift",
        ),
    )
    op.create_index("ix_shift_cost_estimate_run", "shift_cost_estimate", ["forecast_run_id"])
    op.create_index(
        "ix_shift_cost_estimate_employee_date",
        "shift_cost_estimate",
        ["employee_id", "business_date"],
    )

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
                '7d0f7ff8-239a-4a4e-a027-515a3f2b5b8d',
                'schedule.fot_warning_threshold_pct',
                '28.0'::jsonb,
                'number',
                'schedule',
                'Порог предупреждения ФОТ',
                'Порог ФОТ % от выручки для подсветки графика сотрудников.',
                'percent',
                null,
                '%',
                now()
            )
            on conflict (key) do nothing
            """
        )
    )


def downgrade() -> None:
    op.execute("delete from app_setting where key = 'schedule.fot_warning_threshold_pct'")
    op.drop_index("ix_shift_cost_estimate_employee_date", table_name="shift_cost_estimate")
    op.drop_index("ix_shift_cost_estimate_run", table_name="shift_cost_estimate")
    op.drop_table("shift_cost_estimate")
    op.drop_index(
        "ix_payroll_forecast_run_schedule_run_at",
        table_name="payroll_forecast_run",
    )
    op.drop_table("payroll_forecast_run")

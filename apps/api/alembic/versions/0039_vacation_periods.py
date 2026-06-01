"""add vacation periods

Revision ID: 0039_vacation_periods
Revises: 0038_consolidate_hot_section
Create Date: 2026-06-01
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0039_vacation_periods"
down_revision = "0038_consolidate_hot_section"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vacation_period",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("date_start", sa.Date(), nullable=False),
        sa.Column("date_end", sa.Date(), nullable=False),
        sa.Column("days_count", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'planned'"),
        ),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
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
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.CheckConstraint("date_end >= date_start", name="ck_vacation_period_date_range"),
        sa.CheckConstraint(
            "status IN ('planned', 'paid', 'cancelled')",
            name="ck_vacation_period_status",
        ),
        sa.CheckConstraint("days_count > 0", name="ck_vacation_period_days_positive"),
        sa.CheckConstraint(
            "EXTRACT(YEAR FROM date_start) = EXTRACT(YEAR FROM date_end)",
            name="ck_vacation_period_same_year",
        ),
    )
    op.create_index(
        "ix_vacation_period_employee_start",
        "vacation_period",
        ["employee_id", "date_start"],
    )
    op.create_index(
        "ix_vacation_period_date_range",
        "vacation_period",
        ["date_start", "date_end"],
    )
    op.add_column(
        "payroll_line",
        sa.Column(
            "vacation_pay",
            sa.Numeric(14, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
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
            values
                (
                    '1d1a881b-92ac-4cb1-b37f-a774256846d1',
                    'vacation.days_per_year_limit',
                    '20'::jsonb,
                    'number',
                    'vacation',
                    'Лимит отпуска в год',
                    'Количество дней отпуска, доступное сотруднику в календарном году.',
                    'number',
                    null,
                    'дн.',
                    now()
                ),
                (
                    '5bbdff9e-4a81-4d64-9ace-5da7a56bc5a2',
                    'vacation.daily_amount',
                    '1000'::jsonb,
                    'number',
                    'vacation',
                    'Оплата дня отпуска',
                    'Фиксированная сумма, начисляемая за один день отпуска.',
                    'number',
                    null,
                    '₽',
                    now()
                )
            on conflict (key) do nothing
            """
        )
    )


def downgrade() -> None:
    op.execute(
        "delete from app_setting where key in "
        "('vacation.days_per_year_limit', 'vacation.daily_amount')"
    )
    op.drop_column("payroll_line", "vacation_pay")
    op.drop_index("ix_vacation_period_date_range", table_name="vacation_period")
    op.drop_index("ix_vacation_period_employee_start", table_name="vacation_period")
    op.drop_table("vacation_period")

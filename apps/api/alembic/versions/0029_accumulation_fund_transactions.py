"""add accumulation fund transactions

Revision ID: 0029_fund_transactions
Revises: 0028_line_deposit_override
Create Date: 2026-05-31
"""

from __future__ import annotations

import json
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0029_fund_transactions"
down_revision = "0028_line_deposit_override"
branch_labels = None
depends_on = None


NEW_FUND_TIERS = [
    {"min_months": 0, "rate": 0.00},
    {"min_months": 6, "rate": 0.05},
    {"min_months": 12, "rate": 0.10},
    {"min_months": 18, "rate": 0.15},
]

OLD_FUND_TIERS = [
    {"min_years": 0.5, "rate": 0.05},
    {"min_years": 1.0, "rate": 0.10},
    {"min_years": 1.5, "rate": 0.15},
]


def upgrade() -> None:
    op.add_column(
        "employee",
        sa.Column("tenure_started_at", sa.Date(), nullable=True, comment="source=app_managed"),
    )
    op.execute(
        sa.text(
            """
            update employee
               set tenure_started_at = hire_date
             where hire_date is not null
               and tenure_started_at is null
            """
        )
    )

    op.add_column(
        "accumulation_fund_account",
        sa.Column(
            "forfeited_amount",
            sa.Numeric(14, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "accumulation_fund_account",
        sa.Column("forfeited_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "accumulation_fund_account",
        sa.Column("forfeit_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "accumulation_fund_account",
        sa.Column("paid_out_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_accumulation_fund_account_status",
        "accumulation_fund_account",
        "status IN ('active', 'paid_out', 'forfeited')",
    )

    op.create_table(
        "accumulation_fund_transaction",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("transaction_type", sa.String(16), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("rate_percent", sa.Numeric(8, 5), nullable=True),
        sa.Column("base_pay_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accumulation_fund_account.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["payroll_run.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "transaction_type IN ('accrual', 'payout', 'forfeit')",
            name="ck_accumulation_fund_transaction_type",
        ),
        sa.CheckConstraint(
            "amount > 0",
            name="ck_accumulation_fund_transaction_amount_positive",
        ),
    )
    op.create_index(
        "ix_accumulation_fund_transaction_account_created",
        "accumulation_fund_transaction",
        ["account_id", "created_at"],
    )
    op.create_index(
        "ix_accumulation_fund_transaction_employee_year",
        "accumulation_fund_transaction",
        ["employee_id", "year"],
    )

    _upsert_fund_tiers(NEW_FUND_TIERS)


def downgrade() -> None:
    _upsert_fund_tiers(OLD_FUND_TIERS)
    op.drop_index(
        "ix_accumulation_fund_transaction_employee_year",
        table_name="accumulation_fund_transaction",
    )
    op.drop_index(
        "ix_accumulation_fund_transaction_account_created",
        table_name="accumulation_fund_transaction",
    )
    op.drop_table("accumulation_fund_transaction")
    op.drop_constraint(
        "ck_accumulation_fund_account_status",
        "accumulation_fund_account",
        type_="check",
    )
    op.drop_column("accumulation_fund_account", "paid_out_at")
    op.drop_column("accumulation_fund_account", "forfeit_reason")
    op.drop_column("accumulation_fund_account", "forfeited_at")
    op.drop_column("accumulation_fund_account", "forfeited_amount")
    op.drop_column("employee", "tenure_started_at")


def _upsert_fund_tiers(tiers: list[dict[str, float]]) -> None:
    conn = op.get_bind()
    value = json.dumps(tiers)
    result = conn.execute(
        sa.text(
            """
            update app_setting
               set value = cast(:value as jsonb),
                   value_type = 'object',
                   category = coalesce(category, 'Зарплата'),
                   display_name = coalesce(display_name, 'Накопительный фонд'),
                   description = 'Процент накопительного фонда от оклада по стажу.',
                   widget_type = coalesce(widget_type, 'json'),
                   unit = '%'
             where key = 'payroll.fund_rates_by_tenure'
            """
        ),
        {"value": value},
    )
    if result.rowcount:
        return
    conn.execute(
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
                unit
            )
            values (
                cast(:id as uuid),
                'payroll.fund_rates_by_tenure',
                cast(:value as jsonb),
                'object',
                'Зарплата',
                'Накопительный фонд',
                'Процент накопительного фонда от оклада по стажу.',
                'json',
                null,
                '%'
            )
            """
        ),
        {"id": str(uuid.uuid4()), "value": value},
    )

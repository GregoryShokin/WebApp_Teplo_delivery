"""payroll configuration versioned dictionaries

Revision ID: 0007_payroll_configuration
Revises: 0006_employee_category_station
Create Date: 2026-05-27
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0007_payroll_configuration"
down_revision = "0006_employee_category_station"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payroll_rate",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("position_group", sa.String(length=160), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("station", sa.String(length=160), nullable=True),
        sa.Column("rate_type", sa.String(length=24), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "rate_type in ('daily', 'hourly', 'monthly')",
            name="ck_payroll_rate_rate_type",
        ),
        sa.CheckConstraint("amount >= 0", name="ck_payroll_rate_amount_non_negative"),
        sa.CheckConstraint(
            "effective_to is null or effective_to > effective_from",
            name="ck_payroll_rate_effective_range",
        ),
        sa.UniqueConstraint(
            "position_group",
            "category",
            "station",
            "rate_type",
            "effective_from",
            name="uq_payroll_rate_natural_effective_from",
        ),
    )
    op.create_index(
        "ix_payroll_rate_current_lookup",
        "payroll_rate",
        ["position_group", "category", "station", "rate_type", "effective_from", "effective_to"],
    )

    op.create_table(
        "payroll_revenue_share",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("position_group", sa.String(length=160), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("percent", sa.Numeric(8, 5), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "percent >= 0",
            name="ck_payroll_revenue_share_percent_non_negative",
        ),
        sa.CheckConstraint(
            "effective_to is null or effective_to > effective_from",
            name="ck_payroll_revenue_share_effective_range",
        ),
        sa.UniqueConstraint(
            "position_group",
            "category",
            "effective_from",
            name="uq_payroll_revenue_share_natural_effective_from",
        ),
    )
    op.create_index(
        "ix_payroll_revenue_share_current_lookup",
        "payroll_revenue_share",
        ["position_group", "category", "effective_from", "effective_to"],
    )

    op.create_table(
        "payroll_deduction_category",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("code", sa.String(length=96), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("default_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "type in ('fine', 'withholding', 'deposit_writeoff')",
            name="ck_payroll_deduction_category_type",
        ),
        sa.CheckConstraint(
            "default_amount is null or default_amount >= 0",
            name="ck_payroll_deduction_category_default_amount_non_negative",
        ),
        sa.CheckConstraint(
            "effective_to is null or effective_to > effective_from",
            name="ck_payroll_deduction_category_effective_range",
        ),
        sa.UniqueConstraint(
            "code",
            "effective_from",
            name="uq_payroll_deduction_category_code_effective_from",
        ),
    )
    op.create_index(
        "ix_payroll_deduction_category_current_lookup",
        "payroll_deduction_category",
        ["code", "effective_from", "effective_to"],
    )

    op.create_table(
        "payroll_seniority_premium",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("percent_of_base", sa.Numeric(8, 5), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "role in ('senior', 'deputy_senior')",
            name="ck_payroll_seniority_premium_role",
        ),
        sa.CheckConstraint(
            "percent_of_base >= 0",
            name="ck_payroll_seniority_premium_percent_non_negative",
        ),
        sa.CheckConstraint(
            "effective_to is null or effective_to > effective_from",
            name="ck_payroll_seniority_premium_effective_range",
        ),
        sa.UniqueConstraint(
            "role",
            "effective_from",
            name="uq_payroll_seniority_premium_role_effective_from",
        ),
    )
    op.create_index(
        "ix_payroll_seniority_premium_current_lookup",
        "payroll_seniority_premium",
        ["role", "effective_from", "effective_to"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_payroll_seniority_premium_current_lookup",
        table_name="payroll_seniority_premium",
    )
    op.drop_table("payroll_seniority_premium")
    op.drop_index(
        "ix_payroll_deduction_category_current_lookup",
        table_name="payroll_deduction_category",
    )
    op.drop_table("payroll_deduction_category")
    op.drop_index(
        "ix_payroll_revenue_share_current_lookup",
        table_name="payroll_revenue_share",
    )
    op.drop_table("payroll_revenue_share")
    op.drop_index("ix_payroll_rate_current_lookup", table_name="payroll_rate")
    op.drop_table("payroll_rate")

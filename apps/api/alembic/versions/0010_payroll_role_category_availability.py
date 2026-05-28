"""add payroll role category availability

Revision ID: 0010_role_category_availability
Revises: 0009_payroll_categories
Create Date: 2026-05-28
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0010_role_category_availability"
down_revision = "0009_payroll_categories"
branch_labels = None
depends_on = None

CATEGORY_VALUES = ("category_1", "category_2", "category_3", "intern", "freelancer")


def upgrade() -> None:
    op.create_table(
        "payroll_role_category_availability",
        sa.Column("position_group", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.CheckConstraint(
            f"category in {_sql_values(CATEGORY_VALUES)}",
            name="ck_payroll_role_category_availability_category_value",
        ),
        sa.PrimaryKeyConstraint(
            "position_group",
            "category",
            name="pk_payroll_role_category_availability",
        ),
        sa.UniqueConstraint(
            "position_group",
            "category",
            name="uq_payroll_role_category_availability_position_category",
        ),
    )

    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            insert into payroll_role_category_availability (
                position_group,
                category,
                is_enabled
            )
            select position_group,
                   category,
                   bool_or(is_active)
              from payroll_rate
             where rate_type = 'daily'
             group by position_group, category
            """
        )
    )


def downgrade() -> None:
    op.drop_table("payroll_role_category_availability")


def _sql_values(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"

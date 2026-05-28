"""add employee role assignments

Revision ID: 0013_employee_role_assignments
Revises: 0012_weekday_payroll_premium
Create Date: 2026-05-28
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0013_employee_role_assignments"
down_revision = "0012_weekday_payroll_premium"
branch_labels = None
depends_on = None

CATEGORY_VALUES = ("category_1", "category_2", "category_3", "intern", "freelancer")
PAYROLL_ROLE_VALUES = ("sushi", "pizza", "shawarma", "prep", "administrator", "manager")


def upgrade() -> None:
    op.create_table(
        "employee_role_assignment",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payroll_role", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
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
            f"payroll_role in {_sql_values(PAYROLL_ROLE_VALUES)}",
            name="ck_employee_role_assignment_payroll_role_value",
        ),
        sa.CheckConstraint(
            f"category in {_sql_values(CATEGORY_VALUES)}",
            name="ck_employee_role_assignment_category_value",
        ),
        sa.CheckConstraint(
            "effective_to is null or effective_to >= effective_from",
            name="ck_employee_role_assignment_effective_range",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
            name="fk_employee_role_assignment_employee_id_employee",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "employee_id",
            "payroll_role",
            "effective_from",
            name="uq_employee_role_assignment_employee_role_effective_from",
        ),
    )
    op.create_index(
        "ix_employee_role_assignment_employee_active",
        "employee_role_assignment",
        ["employee_id", "effective_from", "effective_to"],
    )
    op.create_index(
        "uq_employee_role_assignment_one_open_primary",
        "employee_role_assignment",
        ["employee_id"],
        unique=True,
        postgresql_where=sa.text("is_primary = true and effective_to is null"),
    )

    _backfill_from_employee_shortcuts()


def downgrade() -> None:
    op.drop_index(
        "uq_employee_role_assignment_one_open_primary",
        table_name="employee_role_assignment",
    )
    op.drop_index(
        "ix_employee_role_assignment_employee_active",
        table_name="employee_role_assignment",
    )
    op.drop_table("employee_role_assignment")


def _backfill_from_employee_shortcuts() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            """
            select id as employee_id,
                   category,
                   coalesce(hire_date, current_date) as effective_from,
                   case
                       when default_cooking_station in ('sushi', 'pizza', 'shawarma')
                           then default_cooking_station
                       when replace(lower(coalesce(position, '')), 'ё', 'е') like '%суш%'
                           then 'sushi'
                       when replace(lower(coalesce(position, '')), 'ё', 'е') like '%пиц%'
                           then 'pizza'
                       when replace(lower(coalesce(position, '')), 'ё', 'е') like '%шаур%'
                           then 'shawarma'
                       when replace(lower(coalesce(position, '')), 'ё', 'е') like '%заготов%'
                           then 'prep'
                       when replace(lower(coalesce(position, '')), 'ё', 'е') like '%администратор%'
                           then 'administrator'
                       when replace(lower(coalesce(position, '')), 'ё', 'е') like '%кассир%'
                           then 'administrator'
                       when replace(lower(coalesce(position, '')), 'ё', 'е') like '%управля%'
                           then 'manager'
                       else null
                   end as payroll_role
              from employee
             where category is not null
            """
        )
    ).mappings()
    assignments = [
        {
            "id": uuid.uuid4(),
            "employee_id": row["employee_id"],
            "payroll_role": row["payroll_role"],
            "category": row["category"],
            "is_primary": True,
            "effective_from": row["effective_from"],
            "effective_to": None,
        }
        for row in rows
        if row["payroll_role"] is not None
    ]
    if not assignments:
        return

    assignment_table = sa.table(
        "employee_role_assignment",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("employee_id", postgresql.UUID(as_uuid=True)),
        sa.column("payroll_role", sa.Text()),
        sa.column("category", sa.Text()),
        sa.column("is_primary", sa.Boolean()),
        sa.column("effective_from", sa.Date()),
        sa.column("effective_to", sa.Date()),
    )
    op.bulk_insert(assignment_table, assignments)


def _sql_values(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"

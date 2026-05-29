"""add category_4 for shawarma payroll

Revision ID: 0016_add_category_4
Revises: 0015_fix_category_2_coefficient
Create Date: 2026-05-28
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0016_add_category_4"
down_revision = "0015_fix_category_2_coefficient"
branch_labels = None
depends_on = None

EFFECTIVE_FROM = date(2026, 5, 28)
OLD_CATEGORY_VALUES = ("category_1", "category_2", "category_3", "intern", "freelancer")
CATEGORY_VALUES = (
    "category_1",
    "category_2",
    "category_3",
    "category_4",
    "intern",
    "freelancer",
)


def upgrade() -> None:
    _replace_category_constraints(CATEGORY_VALUES)
    _seed_category_coefficient()
    _seed_shawarma_rate()
    _seed_category_availability()


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            update employee_role_assignment
               set category = 'category_3'
             where category = 'category_4'
            """
        )
    )
    conn.execute(
        sa.text(
            """
            update employee
               set category = 'category_3'
             where category = 'category_4'
            """
        )
    )
    conn.execute(
        sa.text(
            """
            delete from payroll_role_category_availability
             where category = 'category_4'
            """
        )
    )
    conn.execute(sa.text("delete from payroll_rate where category = 'category_4'"))
    conn.execute(sa.text("delete from category_coefficient where category = 'category_4'"))
    _replace_category_constraints(OLD_CATEGORY_VALUES)


def _replace_category_constraints(values: tuple[str, ...]) -> None:
    op.drop_constraint("ck_employee_category_value", "employee", type_="check")
    op.drop_constraint(
        "ck_employee_role_assignment_category_value",
        "employee_role_assignment",
        type_="check",
    )
    op.drop_constraint("ck_payroll_rate_category_value", "payroll_rate", type_="check")
    op.drop_constraint(
        "ck_payroll_role_category_availability_category_value",
        "payroll_role_category_availability",
        type_="check",
    )
    op.drop_constraint(
        "ck_category_coefficient_category_value",
        "category_coefficient",
        type_="check",
    )

    op.create_check_constraint(
        "ck_employee_category_value",
        "employee",
        f"category is null or category in {_sql_values(values)}",
    )
    op.create_check_constraint(
        "ck_employee_role_assignment_category_value",
        "employee_role_assignment",
        f"category in {_sql_values(values)}",
    )
    op.create_check_constraint(
        "ck_payroll_rate_category_value",
        "payroll_rate",
        f"category in {_sql_values(values)}",
    )
    op.create_check_constraint(
        "ck_payroll_role_category_availability_category_value",
        "payroll_role_category_availability",
        f"category in {_sql_values(values)}",
    )
    op.create_check_constraint(
        "ck_category_coefficient_category_value",
        "category_coefficient",
        f"category in {_sql_values(values)}",
    )


def _seed_category_coefficient() -> None:
    category_coefficient = sa.table(
        "category_coefficient",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("category", sa.Text()),
        sa.column("coefficient", sa.Numeric()),
        sa.column("effective_from", sa.Date()),
        sa.column("effective_to", sa.Date()),
    )
    connection = op.get_bind()
    existing_version = connection.execute(
        sa.select(category_coefficient.c.id).where(
            category_coefficient.c.category == "category_4",
            category_coefficient.c.effective_from == EFFECTIVE_FROM,
        )
    ).first()

    if existing_version is not None:
        op.execute(
            sa.update(category_coefficient)
            .where(category_coefficient.c.id == existing_version.id)
            .values(coefficient=Decimal("2.500"), effective_to=None)
        )
        return

    op.bulk_insert(
        category_coefficient,
        [
            {
                "id": uuid.uuid4(),
                "category": "category_4",
                "coefficient": Decimal("2.500"),
                "effective_from": EFFECTIVE_FROM,
                "effective_to": None,
            }
        ],
    )


def _seed_shawarma_rate() -> None:
    conn = op.get_bind()
    params = {
        "id": uuid.uuid4(),
        "position_group": "Шаурмист",
        "category": "category_4",
        "station": None,
        "rate_type": "daily",
        "amount": Decimal("1800"),
        "effective_from": EFFECTIVE_FROM,
    }
    conn.execute(
        sa.text(
            """
            update payroll_rate
               set station = cast(:station as varchar),
                   amount = :amount,
                   is_active = true,
                   effective_to = null
             where position_group = cast(:position_group as varchar)
               and category = cast(:category as varchar)
               and rate_type = cast(:rate_type as varchar)
               and effective_from = :effective_from
               and station is null
            """
        ),
        params,
    )
    conn.execute(
        sa.text(
            """
            insert into payroll_rate (
                id,
                position_group,
                category,
                station,
                rate_type,
                amount,
                is_active,
                effective_from,
                effective_to
            )
            select :id,
                   cast(:position_group as varchar),
                   cast(:category as varchar),
                   cast(:station as varchar),
                   cast(:rate_type as varchar),
                   :amount,
                   true,
                   :effective_from,
                   null
             where not exists (
                   select 1
                     from payroll_rate
                    where position_group = cast(:position_group as varchar)
                      and category = cast(:category as varchar)
                      and rate_type = cast(:rate_type as varchar)
                      and effective_from = :effective_from
                      and station is null
             )
            """
        ),
        params,
    )


def _seed_category_availability() -> None:
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
                   'category_4',
                   position_group = 'Шаурмист'
              from (
                    select distinct position_group from payroll_rate
                    union
                    select distinct position_group from payroll_role_category_availability
              ) as positions
             where position_group is not null
            on conflict (position_group, category)
            do update set is_enabled = excluded.is_enabled
            """
        )
    )


def _sql_values(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"

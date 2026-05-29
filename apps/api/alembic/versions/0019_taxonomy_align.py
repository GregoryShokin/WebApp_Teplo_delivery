"""align payroll taxonomy availability

Revision ID: 0019_taxonomy_align
Revises: 0018_pin_for_shift
Create Date: 2026-05-29
"""

from __future__ import annotations

import uuid
from datetime import date

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0019_taxonomy_align"
down_revision = "0018_pin_for_shift"
branch_labels = None
depends_on = None

EFFECTIVE_FROM = date(2026, 5, 29)
OLD_PAYROLL_ROLE_VALUES = ("sushi", "pizza", "shawarma", "prep", "administrator", "manager")
PAYROLL_ROLE_VALUES = ("sushi", "pizza", "shawarma", "prep", "administrator")
CANONICAL_AVAILABILITY = (
    ("Администратор", "category_1", False),
    ("Администратор", "category_2", True),
    ("Администратор", "category_3", True),
    ("Администратор", "category_4", True),
    ("Администратор", "intern", True),
    ("Администратор", "freelancer", False),
    ("Сушист", "category_1", True),
    ("Сушист", "category_2", True),
    ("Сушист", "category_3", True),
    ("Сушист", "category_4", False),
    ("Сушист", "intern", True),
    ("Сушист", "freelancer", False),
    ("Пиццерист", "category_1", True),
    ("Пиццерист", "category_2", True),
    ("Пиццерист", "category_3", True),
    ("Пиццерист", "category_4", False),
    ("Пиццерист", "intern", True),
    ("Пиццерист", "freelancer", False),
    ("Шаурмист", "category_1", False),
    ("Шаурмист", "category_2", False),
    ("Шаурмист", "category_3", True),
    ("Шаурмист", "category_4", True),
    ("Шаурмист", "intern", True),
    ("Шаурмист", "freelancer", False),
    ("Заготовщик", "category_1", False),
    ("Заготовщик", "category_2", False),
    ("Заготовщик", "category_3", True),
    ("Заготовщик", "category_4", False),
    ("Заготовщик", "intern", False),
    ("Заготовщик", "freelancer", False),
)


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            delete from employee_role_assignment
             where payroll_role = 'manager'
            """
        )
    )
    _replace_assignment_role_constraint(PAYROLL_ROLE_VALUES)
    _upsert_role_category_availability()
    _ensure_admin_category_4_rate_shell()


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            delete from payroll_rate
             where position_group = 'Администратор'
               and category = 'category_4'
               and effective_from = :effective_from
               and amount is null
            """
        ),
        {"effective_from": EFFECTIVE_FROM},
    )
    _replace_assignment_role_constraint(OLD_PAYROLL_ROLE_VALUES)


def _replace_assignment_role_constraint(values: tuple[str, ...]) -> None:
    op.drop_constraint(
        "ck_employee_role_assignment_payroll_role_value",
        "employee_role_assignment",
        type_="check",
    )
    op.create_check_constraint(
        "ck_employee_role_assignment_payroll_role_value",
        "employee_role_assignment",
        f"payroll_role in {_sql_values(values)}",
    )


def _upsert_role_category_availability() -> None:
    conn = op.get_bind()
    for position_group, category, is_enabled in CANONICAL_AVAILABILITY:
        conn.execute(
            sa.text(
                """
                insert into payroll_role_category_availability (
                    position_group,
                    category,
                    is_enabled
                )
                values (
                    :position_group,
                    :category,
                    :is_enabled
                )
                on conflict (position_group, category)
                do update set is_enabled = excluded.is_enabled
                """
            ),
            {
                "position_group": position_group,
                "category": category,
                "is_enabled": is_enabled,
            },
        )


def _ensure_admin_category_4_rate_shell() -> None:
    payroll_rate = sa.table(
        "payroll_rate",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("position_group", sa.String()),
        sa.column("category", sa.String()),
        sa.column("station", sa.String()),
        sa.column("rate_type", sa.String()),
        sa.column("amount", sa.Numeric()),
        sa.column("is_active", sa.Boolean()),
        sa.column("effective_from", sa.Date()),
        sa.column("effective_to", sa.Date()),
    )
    conn = op.get_bind()
    existing = conn.execute(
        sa.select(payroll_rate.c.id).where(
            payroll_rate.c.position_group == "Администратор",
            payroll_rate.c.category == "category_4",
            payroll_rate.c.rate_type == "daily",
            payroll_rate.c.effective_from == EFFECTIVE_FROM,
            payroll_rate.c.station.is_(None),
        )
    ).first()
    if existing is not None:
        return
    op.bulk_insert(
        payroll_rate,
        [
            {
                "id": uuid.uuid4(),
                "position_group": "Администратор",
                "category": "category_4",
                "station": None,
                "rate_type": "daily",
                "amount": None,
                "is_active": True,
                "effective_from": EFFECTIVE_FROM,
                "effective_to": None,
            }
        ],
    )


def _sql_values(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"

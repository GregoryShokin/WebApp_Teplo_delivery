"""normalize payroll category taxonomy

Revision ID: 0009_payroll_categories
Revises: 0008_seed_payroll_configuration
Create Date: 2026-05-28
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import sqlalchemy as sa

from alembic import op

revision = "0009_payroll_categories"
down_revision = "0008_seed_payroll_configuration"
branch_labels = None
depends_on = None

EFFECTIVE_FROM = date(2026, 1, 1)
CATEGORY_VALUES = ("category_1", "category_2", "category_3", "intern", "freelancer")

TARGET_RATES = (
    ("Сушист", "sushi", "category_1", Decimal("2800"), True),
    ("Сушист", "sushi", "category_2", Decimal("2400"), True),
    ("Сушист", "sushi", "category_3", Decimal("2200"), True),
    ("Сушист", "sushi", "intern", Decimal("2000"), True),
    ("Пиццерист", "pizza", "category_1", Decimal("2600"), True),
    ("Пиццерист", "pizza", "category_2", Decimal("2200"), True),
    ("Пиццерист", "pizza", "category_3", Decimal("2000"), True),
    ("Пиццерист", "pizza", "intern", Decimal("2000"), True),
    ("Шаурмист", "shawarma", "category_1", None, False),
    ("Шаурмист", "shawarma", "category_2", None, False),
    ("Шаурмист", "shawarma", "category_3", Decimal("2000"), True),
    ("Шаурмист", "shawarma", "intern", Decimal("1800"), True),
    ("Заготовщик", None, "category_1", None, False),
    ("Заготовщик", None, "category_2", None, False),
    ("Заготовщик", None, "category_3", Decimal("2200"), True),
    ("Заготовщик", None, "intern", None, False),
    ("Администратор", None, "category_1", None, False),
    ("Администратор", None, "category_2", Decimal("2200"), True),
    ("Администратор", None, "category_3", Decimal("2000"), True),
    ("Администратор", None, "intern", Decimal("1800"), True),
)

DOWNGRADE_ONLY_RATES = (
    ("Сушист", None, "6", Decimal("0")),
    ("Шаурмист", None, "5", Decimal("1800")),
)

PLACEHOLDER_RATES = tuple(rate for rate in TARGET_RATES if not rate[4])


def upgrade() -> None:
    op.add_column(
        "payroll_rate",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.alter_column(
        "payroll_rate",
        "amount",
        existing_type=sa.Numeric(14, 2),
        nullable=True,
    )

    conn = op.get_bind()
    _normalize_employee_categories(conn)
    _normalize_seeded_payroll_rates(conn)

    for position_group, station, category, amount, is_active in TARGET_RATES:
        _insert_rate_if_missing(
            conn,
            position_group=position_group,
            station=station,
            category=category,
            amount=amount,
            is_active=is_active,
        )

    op.create_check_constraint(
        "ck_payroll_rate_category_value",
        "payroll_rate",
        f"category in {_sql_values(CATEGORY_VALUES)}",
    )


def downgrade() -> None:
    conn = op.get_bind()
    op.drop_constraint("ck_payroll_rate_category_value", "payroll_rate", type_="check")

    for position_group, station, category, _amount, _is_active in PLACEHOLDER_RATES:
        conn.execute(
            sa.text(
                """
                delete from payroll_rate
                 where position_group = :position_group
                   and category = :category
                   and rate_type = 'daily'
                   and effective_from = :effective_from
                   and is_active = false
                   and amount is null
                   and (
                       (cast(:station as text) is null and station is null)
                       or station = cast(:station as text)
                   )
                """
            ),
            {
                "position_group": position_group,
                "category": category,
                "station": station,
                "effective_from": EFFECTIVE_FROM,
            },
        )

    _restore_legacy_rate_categories(conn)

    for position_group, station, category, amount in DOWNGRADE_ONLY_RATES:
        _insert_rate_if_missing(
            conn,
            position_group=position_group,
            station=station,
            category=category,
            amount=amount,
            is_active=True,
        )

    conn.execute(sa.text("update payroll_rate set amount = 0 where amount is null"))
    op.alter_column(
        "payroll_rate",
        "amount",
        existing_type=sa.Numeric(14, 2),
        nullable=False,
    )
    op.drop_column("payroll_rate", "is_active")


def _normalize_employee_categories(conn: sa.Connection) -> None:
    conn.execute(
        sa.text(
            """
            update employee
               set category = case
                   when category in (
                       'category_1',
                       'category_2',
                       'category_3',
                       'intern',
                       'freelancer'
                   )
                       then category
                   when btrim(category) = '1' then 'category_1'
                   when btrim(category) = '2' then 'category_2'
                   when btrim(category) = '3' then 'category_3'
                   when btrim(category) = '4' then 'intern'
                   when btrim(category) = '6' then 'freelancer'
                   else null
               end
             where category is not null
            """
        )
    )


def _normalize_seeded_payroll_rates(conn: sa.Connection) -> None:
    conn.execute(
        sa.text(
            """
            delete from payroll_rate
             where (position_group = 'Сушист' and category = '6')
                or (position_group = 'Шаурмист' and category = '5')
            """
        )
    )
    conn.execute(
        sa.text(
            """
            update payroll_rate
               set station = case position_group
                   when 'Сушист' then 'sushi'
                   when 'Пиццерист' then 'pizza'
                   when 'Шаурмист' then 'shawarma'
                   else null
               end
             where position_group in (
                   'Сушист',
                   'Пиццерист',
                   'Шаурмист',
                   'Заготовщик',
                   'Администратор'
             )
            """
        )
    )
    conn.execute(
        sa.text(
            """
            update payroll_rate
               set category = case
                   when position_group = 'Заготовщик' and category = '5' then 'category_3'
                   when position_group in ('Сушист', 'Пиццерист') and category = '5' then 'intern'
                   when category = '1' then 'category_1'
                   when category = '2' then 'category_2'
                   when category = '3' then 'category_3'
                   when category = '4' then 'intern'
                   else category
               end
             where category in ('1', '2', '3', '4', '5')
            """
        )
    )
    conn.execute(
        sa.text(
            """
            update payroll_rate
               set amount = target.amount,
                   is_active = target.is_active
              from (
                  values
                  ('Сушист', 'sushi', 'category_1', 2800::numeric, true),
                  ('Сушист', 'sushi', 'category_2', 2400::numeric, true),
                  ('Сушист', 'sushi', 'category_3', 2200::numeric, true),
                  ('Сушист', 'sushi', 'intern', 2000::numeric, true),
                  ('Пиццерист', 'pizza', 'category_1', 2600::numeric, true),
                  ('Пиццерист', 'pizza', 'category_2', 2200::numeric, true),
                  ('Пиццерист', 'pizza', 'category_3', 2000::numeric, true),
                  ('Пиццерист', 'pizza', 'intern', 2000::numeric, true),
                  ('Шаурмист', 'shawarma', 'category_3', 2000::numeric, true),
                  ('Шаурмист', 'shawarma', 'intern', 1800::numeric, true),
                  ('Заготовщик', null, 'category_3', 2200::numeric, true),
                  ('Администратор', null, 'category_2', 2200::numeric, true),
                  ('Администратор', null, 'category_3', 2000::numeric, true),
                  ('Администратор', null, 'intern', 1800::numeric, true)
              ) as target(position_group, station, category, amount, is_active)
             where payroll_rate.position_group = target.position_group
               and payroll_rate.category = target.category
               and payroll_rate.rate_type = 'daily'
               and payroll_rate.effective_from = :effective_from
               and (
                   (target.station is null and payroll_rate.station is null)
                   or payroll_rate.station = target.station
               )
            """
        ),
        {"effective_from": EFFECTIVE_FROM},
    )


def _restore_legacy_rate_categories(conn: sa.Connection) -> None:
    conn.execute(
        sa.text(
            """
            update payroll_rate
               set category = case
                   when position_group = 'Заготовщик' and category = 'category_3' then '5'
                   when position_group in ('Сушист', 'Пиццерист') and category = 'intern' then '5'
                   when category = 'category_1' then '1'
                   when category = 'category_2' then '2'
                   when category = 'category_3' then '3'
                   when category = 'intern' then '4'
                   else category
               end,
                   station = null,
                   is_active = true
             where position_group in (
                   'Сушист',
                   'Пиццерист',
                   'Шаурмист',
                   'Заготовщик',
                   'Администратор'
             )
            """
        )
    )


def _insert_rate_if_missing(
    conn: sa.Connection,
    *,
    position_group: str,
    station: str | None,
    category: str,
    amount: Decimal | None,
    is_active: bool,
) -> None:
    exists = conn.scalar(
        sa.text(
            """
            select 1
              from payroll_rate
             where position_group = :position_group
               and category = :category
               and rate_type = 'daily'
               and effective_from = :effective_from
               and (
                   (cast(:station as text) is null and station is null)
                   or station = cast(:station as text)
               )
             limit 1
            """
        ),
        {
            "position_group": position_group,
            "category": category,
            "station": station,
            "effective_from": EFFECTIVE_FROM,
        },
    )
    if exists:
        return

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
            values (
                :id,
                :position_group,
                :category,
                :station,
                'daily',
                :amount,
                :is_active,
                :effective_from,
                null
            )
            """
        ),
        {
            "id": uuid.uuid4(),
            "position_group": position_group,
            "category": category,
            "station": station,
            "amount": amount,
            "is_active": is_active,
            "effective_from": EFFECTIVE_FROM,
        },
    )


def _sql_values(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"

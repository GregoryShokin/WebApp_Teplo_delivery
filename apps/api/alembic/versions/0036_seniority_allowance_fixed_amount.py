"""seniority allowance fixed amounts

Revision ID: 0036_seniority_allowance_fixed
Revises: 0035_plan_fact_settings
Create Date: 2026-06-01
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0036_seniority_allowance_fixed"
down_revision = "0035_plan_fact_settings"
branch_labels = None
depends_on = None


POSITIONS = ("Повар", "Кассир")


def upgrade() -> None:
    op.add_column(
        "payroll_seniority_premium",
        sa.Column("position", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "payroll_seniority_premium",
        sa.Column("amount", sa.Numeric(14, 2), nullable=True),
    )
    op.drop_constraint(
        "uq_payroll_seniority_premium_role_effective_from",
        "payroll_seniority_premium",
        type_="unique",
    )

    conn = op.get_bind()
    allowance = conn.execute(
        sa.text(
            """
            select
                coalesce((value::jsonb ->> 'senior')::numeric, 500) as senior_amount,
                coalesce((value::jsonb ->> 'deputy_senior')::numeric, 300) as deputy_amount
            from app_setting
            where key = 'payroll.allowances'
            """
        )
    ).mappings().first()
    senior_amount = Decimal(str(allowance["senior_amount"])) if allowance else Decimal("500")
    deputy_amount = (
        Decimal(str(allowance["deputy_amount"])) if allowance else Decimal("300")
    )

    legacy_rows = conn.execute(
        sa.text(
            """
            select role, percent_of_base, effective_from, effective_to
            from payroll_seniority_premium
            where position is null
            order by role, effective_from
            """
        )
    ).mappings().all()
    payroll_seniority_premium = sa.table(
        "payroll_seniority_premium",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("role", sa.String()),
        sa.column("percent_of_base", sa.Numeric()),
        sa.column("position", sa.String()),
        sa.column("amount", sa.Numeric()),
        sa.column("effective_from", sa.Date()),
        sa.column("effective_to", sa.Date()),
    )
    if legacy_rows:
        op.bulk_insert(
            payroll_seniority_premium,
            [
                {
                    "id": uuid.uuid4(),
                    "role": row["role"],
                    "percent_of_base": row["percent_of_base"],
                    "position": position,
                    "amount": senior_amount if row["role"] == "senior" else deputy_amount,
                    "effective_from": row["effective_from"],
                    "effective_to": row["effective_to"],
                }
                for row in legacy_rows
                for position in POSITIONS
            ],
        )
    op.execute("delete from payroll_seniority_premium where position is null")

    op.alter_column("payroll_seniority_premium", "position", nullable=False)
    op.alter_column("payroll_seniority_premium", "amount", nullable=False)
    op.alter_column("payroll_seniority_premium", "percent_of_base", nullable=True)

    op.create_check_constraint(
        "ck_payroll_seniority_premium_position",
        "payroll_seniority_premium",
        "position in ('Повар', 'Кассир')",
    )
    op.create_check_constraint(
        "ck_payroll_seniority_premium_amount_non_negative",
        "payroll_seniority_premium",
        "amount >= 0",
    )

    op.drop_index(
        "ix_payroll_seniority_premium_current_lookup",
        table_name="payroll_seniority_premium",
    )
    op.create_unique_constraint(
        "uq_seniority_premium_position_role_effective_from",
        "payroll_seniority_premium",
        ["position", "role", "effective_from"],
    )
    op.create_index(
        "ix_payroll_seniority_premium_current_lookup",
        "payroll_seniority_premium",
        ["position", "role", "effective_from", "effective_to"],
    )

    _raise_employee_conflicts(conn, "is_senior", "старших")
    _raise_employee_conflicts(conn, "is_deputy_senior", "заместителей старшего")
    op.create_index(
        "uq_employee_active_senior_per_position",
        "employee",
        ["position"],
        unique=True,
        postgresql_where=sa.text("is_senior = true and status = 'active'"),
    )
    op.create_index(
        "uq_employee_active_deputy_senior_per_position",
        "employee",
        ["position"],
        unique=True,
        postgresql_where=sa.text("is_deputy_senior = true and status = 'active'"),
    )

    op.execute("delete from app_setting where key = 'payroll.allowances'")


def downgrade() -> None:
    conn = op.get_bind()
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
                :id,
                'payroll.allowances',
                '{"senior": 500, "deputy_senior": 300}'::jsonb,
                'object',
                'payroll',
                'Надбавки к смене',
                'Доплаты старшему и заместителю старшего за смену.',
                'json',
                null,
                '₽',
                now()
            )
            on conflict (key) do update
            set value = excluded.value,
                value_type = excluded.value_type,
                category = excluded.category,
                display_name = excluded.display_name,
                description = excluded.description,
                widget_type = excluded.widget_type,
                widget_options = excluded.widget_options,
                unit = excluded.unit,
                updated_at = now()
            """
        ),
        {"id": uuid.uuid4()},
    )

    op.drop_index("uq_employee_active_deputy_senior_per_position", table_name="employee")
    op.drop_index("uq_employee_active_senior_per_position", table_name="employee")

    op.drop_index(
        "ix_payroll_seniority_premium_current_lookup",
        table_name="payroll_seniority_premium",
    )
    op.drop_constraint(
        "uq_seniority_premium_position_role_effective_from",
        "payroll_seniority_premium",
        type_="unique",
    )
    op.drop_constraint(
        "ck_payroll_seniority_premium_amount_non_negative",
        "payroll_seniority_premium",
        type_="check",
    )
    op.drop_constraint(
        "ck_payroll_seniority_premium_position",
        "payroll_seniority_premium",
        type_="check",
    )

    conn.execute(
        sa.text(
            """
            delete from payroll_seniority_premium
            where id in (
                select id
                from (
                    select
                        id,
                        row_number() over (
                            partition by role, effective_from
                            order by case when position = 'Повар' then 0 else 1 end, created_at
                        ) as row_number
                    from payroll_seniority_premium
                ) duplicates
                where row_number > 1
            )
            """
        )
    )
    op.execute(
        "update payroll_seniority_premium "
        "set percent_of_base = 0 where percent_of_base is null"
    )
    op.alter_column("payroll_seniority_premium", "percent_of_base", nullable=False)
    op.drop_column("payroll_seniority_premium", "amount")
    op.drop_column("payroll_seniority_premium", "position")

    op.create_unique_constraint(
        "uq_payroll_seniority_premium_role_effective_from",
        "payroll_seniority_premium",
        ["role", "effective_from"],
    )
    op.create_index(
        "ix_payroll_seniority_premium_current_lookup",
        "payroll_seniority_premium",
        ["role", "effective_from", "effective_to"],
    )


def _raise_employee_conflicts(conn: sa.Connection, field: str, label: str) -> None:
    conflicts = conn.execute(
        sa.text(
            f"""
            select position, count(*) as count
            from employee
            where {field} = true and status = 'active'
            group by position
            having count(*) > 1
            order by position
            """
        )
    ).fetchall()
    if conflicts:
        raise Exception(
            f"Нарушения уникальности {label} до миграции: {conflicts}. "
            "Разрулите вручную перед накатом."
        )

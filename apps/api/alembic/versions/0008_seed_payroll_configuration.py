"""seed payroll configuration from source data sheet

Revision ID: 0008_seed_payroll_configuration
Revises: 0007_payroll_configuration
Create Date: 2026-05-27
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0008_seed_payroll_configuration"
down_revision = "0007_payroll_configuration"
branch_labels = None
depends_on = None

EFFECTIVE_FROM = date(2026, 1, 1)


PAYROLL_RATES = [
    ("Администратор", "2", Decimal("2200")),
    ("Администратор", "3", Decimal("2000")),
    ("Администратор", "4", Decimal("1800")),
    ("Заготовщик", "5", Decimal("2200")),
    ("Пиццерист", "1", Decimal("2600")),
    ("Пиццерист", "2", Decimal("2200")),
    ("Пиццерист", "3", Decimal("2000")),
    ("Пиццерист", "5", Decimal("2000")),
    ("Сушист", "1", Decimal("2800")),
    ("Сушист", "2", Decimal("2400")),
    ("Сушист", "3", Decimal("2200")),
    ("Сушист", "5", Decimal("2000")),
    ("Сушист", "6", Decimal("0")),
    ("Шаурмист", "3", Decimal("2000")),
    ("Шаурмист", "4", Decimal("1800")),
    ("Шаурмист", "5", Decimal("1800")),
]

# Source sheet stores revenue percent as daily-revenue tiers, while the first
# target table version has no threshold column. Keep the threshold in a
# human-readable category label until the model is expanded.
REVENUE_SHARES = [
    ("Производство", "выручка от 50 000", Decimal("0.03500")),
    ("Производство", "выручка от 140 000", Decimal("0.04500")),
    ("Производство", "выручка от 190 000", Decimal("0.05500")),
    ("Производство", "выручка от 550 000", Decimal("0.06500")),
]

DEDUCTION_CATEGORIES = [
    (
        "audit_fine_production",
        "Штраф по ревизии для производства",
        "Ставка из строки штрафа по ревизии для производственных ролей.",
        "fine",
        Decimal("929"),
    ),
    (
        "audit_fine_admin",
        "Штраф по ревизии для администратора",
        "Ставка из строки штрафа по ревизии для сменного администратора.",
        "fine",
        Decimal("25"),
    ),
    (
        "manual_withholding",
        "Штрафы и удержания",
        "Нормализованная причина ручных удержаний, включая legacy-вариант с NBSP.",
        "withholding",
        None,
    ),
    (
        "deposit_writeoff",
        "Списание депозитов",
        "Управленческая категория для внутреннего списания депозита.",
        "deposit_writeoff",
        None,
    ),
]

# TODO payroll-config: the sheet has flat seniority allowances (+500/+300 rub),
# but the target table stores percent_of_base. Keep 0 until owner confirms the
# conversion from fixed amount to percentage.
SENIORITY_PREMIUMS = [
    ("senior", Decimal("0.00000")),
    ("deputy_senior", Decimal("0.00000")),
]


def upgrade() -> None:
    payroll_rate_table = sa.table(
        "payroll_rate",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("position_group", sa.String()),
        sa.column("category", sa.String()),
        sa.column("station", sa.String()),
        sa.column("rate_type", sa.String()),
        sa.column("amount", sa.Numeric()),
        sa.column("effective_from", sa.Date()),
        sa.column("effective_to", sa.Date()),
    )
    op.bulk_insert(
        payroll_rate_table,
        [
            {
                "id": uuid.uuid4(),
                "position_group": position_group,
                "category": category,
                "station": None,
                "rate_type": "daily",
                "amount": amount,
                "effective_from": EFFECTIVE_FROM,
                "effective_to": None,
            }
            for position_group, category, amount in PAYROLL_RATES
        ],
    )

    payroll_revenue_share_table = sa.table(
        "payroll_revenue_share",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("position_group", sa.String()),
        sa.column("category", sa.String()),
        sa.column("percent", sa.Numeric()),
        sa.column("effective_from", sa.Date()),
        sa.column("effective_to", sa.Date()),
    )
    op.bulk_insert(
        payroll_revenue_share_table,
        [
            {
                "id": uuid.uuid4(),
                "position_group": position_group,
                "category": category,
                "percent": percent,
                "effective_from": EFFECTIVE_FROM,
                "effective_to": None,
            }
            for position_group, category, percent in REVENUE_SHARES
        ],
    )

    payroll_deduction_category_table = sa.table(
        "payroll_deduction_category",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("code", sa.String()),
        sa.column("display_name", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("type", sa.String()),
        sa.column("default_amount", sa.Numeric()),
        sa.column("effective_from", sa.Date()),
        sa.column("effective_to", sa.Date()),
    )
    op.bulk_insert(
        payroll_deduction_category_table,
        [
            {
                "id": uuid.uuid4(),
                "code": code,
                "display_name": display_name,
                "description": description,
                "type": type_,
                "default_amount": default_amount,
                "effective_from": EFFECTIVE_FROM,
                "effective_to": None,
            }
            for code, display_name, description, type_, default_amount in DEDUCTION_CATEGORIES
        ],
    )

    payroll_seniority_premium_table = sa.table(
        "payroll_seniority_premium",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("role", sa.String()),
        sa.column("percent_of_base", sa.Numeric()),
        sa.column("effective_from", sa.Date()),
        sa.column("effective_to", sa.Date()),
    )
    op.bulk_insert(
        payroll_seniority_premium_table,
        [
            {
                "id": uuid.uuid4(),
                "role": role,
                "percent_of_base": percent_of_base,
                "effective_from": EFFECTIVE_FROM,
                "effective_to": None,
            }
            for role, percent_of_base in SENIORITY_PREMIUMS
        ],
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "delete from payroll_seniority_premium "
            "where effective_from = :effective_from and role = any(:roles)"
        ),
        {
            "effective_from": EFFECTIVE_FROM,
            "roles": [role for role, _percent in SENIORITY_PREMIUMS],
        },
    )
    conn.execute(
        sa.text(
            "delete from payroll_deduction_category "
            "where effective_from = :effective_from and code = any(:codes)"
        ),
        {
            "effective_from": EFFECTIVE_FROM,
            "codes": [code for code, *_rest in DEDUCTION_CATEGORIES],
        },
    )
    conn.execute(
        sa.text(
            "delete from payroll_revenue_share "
            "where effective_from = :effective_from and position_group = :position_group"
        ),
        {"effective_from": EFFECTIVE_FROM, "position_group": "Производство"},
    )
    conn.execute(
        sa.text(
            "delete from payroll_rate "
            "where effective_from = :effective_from and position_group = any(:position_groups)"
        ),
        {
            "effective_from": EFFECTIVE_FROM,
            "position_groups": sorted({position for position, _category, _amount in PAYROLL_RATES}),
        },
    )

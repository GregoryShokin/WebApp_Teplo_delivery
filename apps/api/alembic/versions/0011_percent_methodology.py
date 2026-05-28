"""add revenue-pool percent methodology

Revision ID: 0011_percent_methodology
Revises: 0010_role_category_availability
Create Date: 2026-05-28
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0011_percent_methodology"
down_revision = "0010_role_category_availability"
branch_labels = None
depends_on = None

EFFECTIVE_FROM = date(2026, 1, 1)
CATEGORY_VALUES = ("category_1", "category_2", "category_3", "intern", "freelancer")

REVENUE_TIERS = (
    (Decimal("50000"), Decimal("140000"), Decimal("0.03500")),
    (Decimal("140000"), Decimal("190000"), Decimal("0.04500")),
    (Decimal("190000"), Decimal("550000"), Decimal("0.05500")),
    (Decimal("550000"), None, Decimal("0.06500")),
)

CATEGORY_COEFFICIENTS = (
    ("category_1", Decimal("3.000")),
    ("category_2", Decimal("2.000")),
    ("category_3", Decimal("1.500")),
    ("intern", Decimal("0.000")),
    ("freelancer", Decimal("0.000")),
)


def upgrade() -> None:
    op.create_table(
        "revenue_tier",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("min_revenue", sa.Numeric(14, 2), nullable=False),
        sa.Column("max_revenue", sa.Numeric(14, 2), nullable=True),
        sa.Column("rate_percent", sa.Numeric(8, 5), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "min_revenue >= 0",
            name="ck_revenue_tier_min_revenue_non_negative",
        ),
        sa.CheckConstraint(
            "max_revenue is null or max_revenue > min_revenue",
            name="ck_revenue_tier_revenue_range",
        ),
        sa.CheckConstraint(
            "rate_percent >= 0",
            name="ck_revenue_tier_rate_percent_non_negative",
        ),
        sa.CheckConstraint(
            "effective_to is null or effective_to > effective_from",
            name="ck_revenue_tier_effective_range",
        ),
        sa.UniqueConstraint(
            "min_revenue",
            "effective_from",
            name="uq_revenue_tier_min_revenue_effective_from",
        ),
    )
    op.create_index(
        "ix_revenue_tier_current_lookup",
        "revenue_tier",
        ["min_revenue", "max_revenue", "effective_from", "effective_to"],
    )

    op.create_table(
        "category_coefficient",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("coefficient", sa.Numeric(8, 3), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            f"category in {_sql_values(CATEGORY_VALUES)}",
            name="ck_category_coefficient_category_value",
        ),
        sa.CheckConstraint(
            "coefficient >= 0",
            name="ck_category_coefficient_coefficient_non_negative",
        ),
        sa.CheckConstraint(
            "effective_to is null or effective_to > effective_from",
            name="ck_category_coefficient_effective_range",
        ),
        sa.UniqueConstraint(
            "category",
            "effective_from",
            name="uq_category_coefficient_category_effective_from",
        ),
    )
    op.create_index(
        "ix_category_coefficient_current_lookup",
        "category_coefficient",
        ["category", "effective_from", "effective_to"],
    )

    _seed_revenue_tiers()
    _seed_category_coefficients()


def downgrade() -> None:
    op.drop_index(
        "ix_category_coefficient_current_lookup",
        table_name="category_coefficient",
    )
    op.drop_table("category_coefficient")
    op.drop_index("ix_revenue_tier_current_lookup", table_name="revenue_tier")
    op.drop_table("revenue_tier")


def _seed_revenue_tiers() -> None:
    revenue_tier_table = sa.table(
        "revenue_tier",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("min_revenue", sa.Numeric()),
        sa.column("max_revenue", sa.Numeric()),
        sa.column("rate_percent", sa.Numeric()),
        sa.column("effective_from", sa.Date()),
        sa.column("effective_to", sa.Date()),
    )
    op.bulk_insert(
        revenue_tier_table,
        [
            {
                "id": uuid.uuid4(),
                "min_revenue": min_revenue,
                "max_revenue": max_revenue,
                "rate_percent": rate_percent,
                "effective_from": EFFECTIVE_FROM,
                "effective_to": None,
            }
            for min_revenue, max_revenue, rate_percent in REVENUE_TIERS
        ],
    )


def _seed_category_coefficients() -> None:
    category_coefficient_table = sa.table(
        "category_coefficient",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("category", sa.Text()),
        sa.column("coefficient", sa.Numeric()),
        sa.column("effective_from", sa.Date()),
        sa.column("effective_to", sa.Date()),
    )
    op.bulk_insert(
        category_coefficient_table,
        [
            {
                "id": uuid.uuid4(),
                "category": category,
                "coefficient": coefficient,
                "effective_from": EFFECTIVE_FROM,
                "effective_to": None,
            }
            for category, coefficient in CATEGORY_COEFFICIENTS
        ],
    )


def _sql_values(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"

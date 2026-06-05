"""rebaseline percent category coefficients

Revision ID: 0062_rebaseline_percent_coefficients
Revises: 0061_rbac_permissions
Create Date: 2026-06-05
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0062_rebaseline_percent_coefficients"
down_revision = "0061_rbac_permissions"
branch_labels = None
depends_on = None

EFFECTIVE_FROM = date(2026, 1, 1)
OLD_CATEGORY_2_EFFECTIVE_FROM = date(2026, 5, 28)

CATEGORY_COEFFICIENTS = (
    ("category_1", Decimal("10.000"), EFFECTIVE_FROM, None),
    ("category_2", Decimal("7.500"), EFFECTIVE_FROM, None),
    ("category_3", Decimal("5.000"), EFFECTIVE_FROM, None),
    ("category_4", Decimal("2.500"), EFFECTIVE_FROM, None),
    ("intern", Decimal("0.000"), EFFECTIVE_FROM, None),
    ("freelancer", Decimal("0.000"), EFFECTIVE_FROM, None),
)

PREVIOUS_CATEGORY_COEFFICIENTS = (
    ("category_1", Decimal("3.000"), EFFECTIVE_FROM, None),
    ("category_2", Decimal("2.000"), EFFECTIVE_FROM, OLD_CATEGORY_2_EFFECTIVE_FROM),
    ("category_2", Decimal("2.250"), OLD_CATEGORY_2_EFFECTIVE_FROM, None),
    ("category_3", Decimal("1.500"), EFFECTIVE_FROM, None),
    ("category_4", Decimal("2.500"), OLD_CATEGORY_2_EFFECTIVE_FROM, None),
    ("intern", Decimal("0.000"), EFFECTIVE_FROM, None),
    ("freelancer", Decimal("0.000"), EFFECTIVE_FROM, None),
)


def upgrade() -> None:
    category_coefficient = _category_coefficient_table()
    _replace_coefficients(category_coefficient, CATEGORY_COEFFICIENTS)


def downgrade() -> None:
    category_coefficient = _category_coefficient_table()
    _replace_coefficients(category_coefficient, PREVIOUS_CATEGORY_COEFFICIENTS)


def _replace_coefficients(
    category_coefficient: sa.Table,
    coefficients: tuple[tuple[str, Decimal, date, date | None], ...],
) -> None:
    categories = {category for category, *_ in coefficients}
    op.execute(
        sa.delete(category_coefficient).where(category_coefficient.c.category.in_(categories))
    )
    op.bulk_insert(
        category_coefficient,
        [
            {
                "id": uuid.uuid4(),
                "category": category,
                "coefficient": coefficient,
                "effective_from": effective_from,
                "effective_to": effective_to,
            }
            for category, coefficient, effective_from, effective_to in coefficients
        ],
    )


def _category_coefficient_table() -> sa.Table:
    return sa.table(
        "category_coefficient",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("category", sa.Text()),
        sa.column("coefficient", sa.Numeric()),
        sa.column("effective_from", sa.Date()),
        sa.column("effective_to", sa.Date()),
    )

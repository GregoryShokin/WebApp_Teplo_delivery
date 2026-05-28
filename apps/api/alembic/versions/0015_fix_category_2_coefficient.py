"""fix category_2 percent coefficient

Revision ID: 0015_fix_category_2_coefficient
Revises: 0014_shift_ledger
Create Date: 2026-05-28
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0015_fix_category_2_coefficient"
down_revision = "0014_shift_ledger"
branch_labels = None
depends_on = None

EFFECTIVE_FROM = date(2026, 5, 28)
CATEGORY = "category_2"
COEFFICIENT = Decimal("2.250")


def upgrade() -> None:
    category_coefficient = _category_coefficient_table()
    connection = op.get_bind()

    existing_version = connection.execute(
        sa.select(category_coefficient.c.id).where(
            category_coefficient.c.category == CATEGORY,
            category_coefficient.c.effective_from == EFFECTIVE_FROM,
        )
    ).first()

    op.execute(
        sa.update(category_coefficient)
        .where(
            category_coefficient.c.category == CATEGORY,
            category_coefficient.c.effective_from < EFFECTIVE_FROM,
            sa.or_(
                category_coefficient.c.effective_to.is_(None),
                category_coefficient.c.effective_to > EFFECTIVE_FROM,
            ),
        )
        .values(effective_to=EFFECTIVE_FROM)
    )

    if existing_version is not None:
        op.execute(
            sa.update(category_coefficient)
            .where(category_coefficient.c.id == existing_version.id)
            .values(coefficient=COEFFICIENT, effective_to=None)
        )
        return

    op.bulk_insert(
        category_coefficient,
        [
            {
                "id": uuid.uuid4(),
                "category": CATEGORY,
                "coefficient": COEFFICIENT,
                "effective_from": EFFECTIVE_FROM,
                "effective_to": None,
            }
        ],
    )


def downgrade() -> None:
    category_coefficient = _category_coefficient_table()
    op.execute(
        sa.delete(category_coefficient).where(
            category_coefficient.c.category == CATEGORY,
            category_coefficient.c.effective_from == EFFECTIVE_FROM,
            category_coefficient.c.coefficient == COEFFICIENT,
        )
    )
    op.execute(
        sa.update(category_coefficient)
        .where(
            category_coefficient.c.category == CATEGORY,
            category_coefficient.c.effective_to == EFFECTIVE_FROM,
        )
        .values(effective_to=None)
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

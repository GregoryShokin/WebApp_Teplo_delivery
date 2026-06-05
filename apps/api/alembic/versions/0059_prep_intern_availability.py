"""allow prep intern category

Revision ID: 0059_prep_intern_availability
Revises: 0058_delivery_timestamps
Create Date: 2026-06-05
"""

from __future__ import annotations

import uuid
from datetime import date

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0059_prep_intern_availability"
down_revision = "0058_delivery_timestamps"
branch_labels = None
depends_on = None

EFFECTIVE_FROM = date(2026, 1, 1)


def upgrade() -> None:
    _set_prep_intern_availability(True)
    _set_prep_intern_rate(amount=2200, is_active=True)


def downgrade() -> None:
    _set_prep_intern_rate(amount=None, is_active=False)
    _set_prep_intern_availability(False)


def _set_prep_intern_availability(is_enabled: bool) -> None:
    op.execute(
        sa.text(
            """
            insert into payroll_role_category_availability (
                position_group,
                category,
                is_enabled
            )
            values ('Заготовщик', 'intern', :is_enabled)
            on conflict (position_group, category)
            do update set is_enabled = excluded.is_enabled
            """
        ).bindparams(is_enabled=is_enabled)
    )


def _set_prep_intern_rate(*, amount: int | None, is_active: bool) -> None:
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
            payroll_rate.c.position_group == "Заготовщик",
            payroll_rate.c.category == "intern",
            payroll_rate.c.rate_type == "daily",
            payroll_rate.c.effective_from == EFFECTIVE_FROM,
            payroll_rate.c.station.is_(None),
        )
    ).first()
    if existing is not None:
        conn.execute(
            payroll_rate.update()
            .where(payroll_rate.c.id == existing.id)
            .values(amount=amount, is_active=is_active)
        )
        return

    op.bulk_insert(
        payroll_rate,
        [
            {
                "id": uuid.uuid4(),
                "position_group": "Заготовщик",
                "category": "intern",
                "station": None,
                "rate_type": "daily",
                "amount": amount,
                "is_active": is_active,
                "effective_from": EFFECTIVE_FROM,
                "effective_to": None,
            }
        ],
    )

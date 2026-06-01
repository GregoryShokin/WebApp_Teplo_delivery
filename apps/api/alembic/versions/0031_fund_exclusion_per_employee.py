"""add fund exclusion per employee

Revision ID: 0031_fund_exclusion
Revises: 0030_fund_initial_balance
Create Date: 2026-06-01
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0031_fund_exclusion"
down_revision = "0030_fund_initial_balance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employee",
        sa.Column(
            "fund_excluded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="source=app_managed",
        ),
    )
    op.add_column(
        "employee",
        sa.Column(
            "fund_excluded_until",
            sa.Date(),
            nullable=True,
            comment="source=app_managed",
        ),
    )
    op.add_column(
        "employee",
        sa.Column(
            "fund_excluded_reason",
            sa.Text(),
            nullable=True,
            comment="source=app_managed",
        ),
    )


def downgrade() -> None:
    op.drop_column("employee", "fund_excluded_reason")
    op.drop_column("employee", "fund_excluded_until")
    op.drop_column("employee", "fund_excluded")

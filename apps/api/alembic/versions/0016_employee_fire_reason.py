"""add employee dismissal reason

Revision ID: 0016_employee_fire_reason
Revises: 0016_add_category_4
Create Date: 2026-05-28
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0016_employee_fire_reason"
down_revision = "0016_add_category_4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employee",
        sa.Column("fire_reason", sa.Text(), nullable=True, comment="source=app_managed"),
    )


def downgrade() -> None:
    op.drop_column("employee", "fire_reason")

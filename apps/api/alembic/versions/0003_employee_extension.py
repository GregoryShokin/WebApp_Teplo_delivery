"""extend employee master for staff page and iiko sync

Revision ID: 0003_employee_extension
Revises: 0002_seed_reference
Create Date: 2026-05-27
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003_employee_extension"
down_revision = "0002_seed_reference"
branch_labels = None
depends_on = None


EMPLOYEE_STATUS = postgresql.ENUM(
    "active",
    "inactive",
    "needs_setup",
    name="employee_status",
)


def upgrade() -> None:
    bind = op.get_bind()
    EMPLOYEE_STATUS.create(bind, checkfirst=True)

    op.add_column(
        "employee",
        sa.Column(
            "is_senior",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="source=app_managed",
        ),
    )
    op.add_column(
        "employee",
        sa.Column(
            "is_deputy_senior",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="source=app_managed",
        ),
    )
    op.add_column(
        "employee",
        sa.Column(
            "iiko_sync_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="source=iiko",
        ),
    )
    op.execute("update employee set status = 'active' where status is null or status = ''")
    op.alter_column(
        "employee",
        "status",
        existing_type=sa.String(length=64),
        type_=postgresql.ENUM(name="employee_status", create_type=False),
        existing_nullable=False,
        server_default=sa.text("'active'"),
        postgresql_using="status::employee_status",
    )


def downgrade() -> None:
    op.alter_column(
        "employee",
        "status",
        existing_type=postgresql.ENUM(name="employee_status", create_type=False),
        type_=sa.String(length=64),
        existing_nullable=False,
        server_default=None,
        postgresql_using="status::text",
    )
    op.drop_column("employee", "iiko_sync_at")
    op.drop_column("employee", "is_deputy_senior")
    op.drop_column("employee", "is_senior")

    bind = op.get_bind()
    EMPLOYEE_STATUS.drop(bind, checkfirst=True)

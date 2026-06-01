"""add employee PIN origin flag

Revision ID: 0022_pin_origin
Revises: 0022_aux_staff_positions
Create Date: 2026-05-30
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0022_pin_origin"
down_revision = "0022_aux_staff_positions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employee",
        sa.Column(
            "pin_assumed_from_iiko",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="source=app_managed",
        ),
    )
    op.execute(
        sa.text(
            """
            update employee
               set pin_assumed_from_iiko = true
             where iiko_id is not null
               and pin_hash is null
            """
        )
    )
    op.create_check_constraint(
        op.f("ck_employee_pin_origin_exclusive"),
        "employee",
        "not (pin_assumed_from_iiko = true and pin_hash is not null)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_employee_pin_origin_exclusive"), "employee", type_="check")
    op.drop_column("employee", "pin_assumed_from_iiko")

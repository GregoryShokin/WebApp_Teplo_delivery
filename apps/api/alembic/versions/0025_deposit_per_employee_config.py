"""add per employee deposit configuration

Revision ID: 0025_deposit_per_employee_config
Revises: 0025_weekday_premium_shift_unit
Create Date: 2026-05-31
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0025_deposit_per_employee_config"
down_revision = "0025_weekday_premium_shift_unit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employee",
        sa.Column(
            "deposit_target_override",
            sa.Numeric(14, 2),
            nullable=True,
            comment="source=app_managed",
        ),
    )
    op.add_column(
        "employee",
        sa.Column(
            "deposit_withholding_override",
            sa.Numeric(14, 2),
            nullable=True,
            comment="source=app_managed",
        ),
    )
    op.add_column(
        "employee",
        sa.Column(
            "deposit_excluded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="source=app_managed",
        ),
    )
    op.add_column(
        "employee",
        sa.Column(
            "deposit_excluded_until",
            sa.Date(),
            nullable=True,
            comment="source=app_managed",
        ),
    )
    op.add_column(
        "employee",
        sa.Column(
            "deposit_excluded_reason",
            sa.Text(),
            nullable=True,
            comment="source=app_managed",
        ),
    )
    op.create_check_constraint(
        op.f("ck_employee_deposit_target_override_non_negative"),
        "employee",
        "deposit_target_override is null or deposit_target_override >= 0",
    )
    op.create_check_constraint(
        op.f("ck_employee_deposit_withholding_override_non_negative"),
        "employee",
        "deposit_withholding_override is null or deposit_withholding_override >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_employee_deposit_withholding_override_non_negative"),
        "employee",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_employee_deposit_target_override_non_negative"),
        "employee",
        type_="check",
    )
    op.drop_column("employee", "deposit_excluded_reason")
    op.drop_column("employee", "deposit_excluded_until")
    op.drop_column("employee", "deposit_excluded")
    op.drop_column("employee", "deposit_withholding_override")
    op.drop_column("employee", "deposit_target_override")

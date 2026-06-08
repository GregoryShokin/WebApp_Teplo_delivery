"""add payroll manual adjustments

Revision ID: 0027_payroll_adjustments
Revises: 0026_deposit_initial_balance
Create Date: 2026-05-31
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0027_payroll_adjustments"
down_revision = "0026_deposit_initial_balance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payroll_adjustment_category",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("code", sa.String(length=96), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("default_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "type in ('bonus', 'penalty')", name="ck_payroll_adjustment_category_type"
        ),
        sa.CheckConstraint(
            "default_amount is null or default_amount >= 0",
            name="ck_payroll_adjustment_category_default_amount_non_negative",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_payroll_adjustment_category_code"),
    )
    op.create_table(
        "payroll_adjustment",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("custom_label", sa.String(length=200), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_label", sa.String(length=160), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("type in ('bonus', 'penalty')", name="ck_payroll_adjustment_type"),
        sa.CheckConstraint("amount > 0", name="ck_payroll_adjustment_amount_positive"),
        sa.CheckConstraint(
            "category_id is not null or custom_label is not null",
            name="ck_payroll_adjustment_category_or_custom",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"], ["payroll_adjustment_category.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_payroll_adjustment_employee_date",
        "payroll_adjustment",
        ["employee_id", "work_date"],
    )
    op.create_index("ix_payroll_adjustment_work_date", "payroll_adjustment", ["work_date"])

    category_table = sa.table(
        "payroll_adjustment_category",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("type", sa.String(length=16)),
        sa.column("code", sa.String(length=96)),
        sa.column("display_name", sa.String(length=200)),
        sa.column("default_amount", sa.Numeric(14, 2)),
        sa.column("is_active", sa.Boolean()),
        sa.column("sort_order", sa.Integer()),
    )
    op.bulk_insert(
        category_table,
        [
            {
                "id": uuid.UUID("2d89f4d6-492f-4b78-a6e8-0c6ed530f732"),
                "type": "penalty",
                "code": "late_arrival",
                "display_name": "Опоздание",
                "default_amount": Decimal("500.00"),
                "is_active": True,
                "sort_order": 10,
            },
            {
                "id": uuid.UUID("93efa0bc-64f6-4512-9939-4fb3918725f9"),
                "type": "penalty",
                "code": "order_error",
                "display_name": "Ошибка в заказе",
                "default_amount": Decimal("300.00"),
                "is_active": True,
                "sort_order": 20,
            },
            {
                "id": uuid.UUID("e612dfc8-f2bb-4399-81a4-3fb8cb8197f1"),
                "type": "penalty",
                "code": "cleanliness_violation",
                "display_name": "Нарушение чистоты",
                "default_amount": Decimal("500.00"),
                "is_active": True,
                "sort_order": 30,
            },
            {
                "id": uuid.UUID("7ba58195-70f6-40cf-8d63-1ebafcc49d99"),
                "type": "bonus",
                "code": "quality_work",
                "display_name": "Качественная работа",
                "default_amount": Decimal("1000.00"),
                "is_active": True,
                "sort_order": 10,
            },
            {
                "id": uuid.UUID("9d711829-b51c-49a5-a973-bfc23cf16b41"),
                "type": "bonus",
                "code": "plan_overachievement",
                "display_name": "Перевыполнение плана",
                "default_amount": Decimal("1500.00"),
                "is_active": True,
                "sort_order": 20,
            },
            {
                "id": uuid.UUID("a41d9109-ed62-4608-8d21-4f4896567f98"),
                "type": "bonus",
                "code": "shift_replacement",
                "display_name": "Замена смены",
                "default_amount": Decimal("1000.00"),
                "is_active": True,
                "sort_order": 30,
            },
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_payroll_adjustment_work_date", table_name="payroll_adjustment")
    op.drop_index("ix_payroll_adjustment_employee_date", table_name="payroll_adjustment")
    op.drop_table("payroll_adjustment")
    op.drop_table("payroll_adjustment_category")

"""extend payroll payments for payout splits

Revision ID: 0074_payroll_payouts
Revises: 0073_payroll_payment
Create Date: 2026-06-07
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.auth.permissions import (
    ALL_PERMISSION_CODES,
    DEFAULT_ROLE_PERMISSIONS,
    FULL_ACCESS_ROLE_CODES,
    PERMISSION_CATALOG,
)

revision = "0074_payroll_payouts"
down_revision = "0073_payroll_payment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payroll_payment",
        sa.Column(
            "amount_cash",
            sa.Numeric(14, 2),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "payroll_payment",
        sa.Column(
            "amount_account",
            sa.Numeric(14, 2),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "payroll_payment",
        sa.Column("status", sa.String(length=16), nullable=False, server_default="paid"),
    )
    op.add_column(
        "payroll_payment",
        sa.Column("draft_document_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "payroll_payment",
        sa.Column("draft_status", sa.String(length=24), nullable=True),
    )
    op.add_column(
        "payroll_payment",
        sa.Column("draft_synced_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "payroll_payment",
        sa.Column(
            "overpaid_amount",
            sa.Numeric(14, 2),
            nullable=False,
            server_default="0",
        ),
    )
    op.execute("update payroll_payment set amount_account = amount, amount_cash = 0, status = 'paid'")

    op.drop_constraint("ck_payroll_payment_method", "payroll_payment", type_="check")
    op.alter_column("payroll_payment", "paid_at", existing_type=sa.Date(), nullable=True)
    op.alter_column(
        "payroll_payment",
        "method",
        existing_type=sa.String(length=32),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_payroll_payment_method",
        "payroll_payment",
        "method is null or method in ('business_card', 'cash', 'transfer', 'other')",
    )
    op.create_check_constraint(
        "ck_payroll_payment_amount_cash_non_negative",
        "payroll_payment",
        "amount_cash >= 0",
    )
    op.create_check_constraint(
        "ck_payroll_payment_amount_account_non_negative",
        "payroll_payment",
        "amount_account >= 0",
    )
    op.create_check_constraint(
        "ck_payroll_payment_overpaid_amount_non_negative",
        "payroll_payment",
        "overpaid_amount >= 0",
    )
    op.create_check_constraint(
        "ck_payroll_payment_status",
        "payroll_payment",
        "status in ('planned', 'draft_created', 'paid')",
    )
    _upsert_permissions()
    _grant_permissions(tuple(FULL_ACCESS_ROLE_CODES), tuple(ALL_PERMISSION_CODES))
    for role_code, permission_codes in DEFAULT_ROLE_PERMISSIONS.items():
        _grant_permissions((role_code,), tuple(permission_codes))


def downgrade() -> None:
    op.drop_constraint("ck_payroll_payment_status", "payroll_payment", type_="check")
    op.drop_constraint(
        "ck_payroll_payment_overpaid_amount_non_negative",
        "payroll_payment",
        type_="check",
    )
    op.drop_constraint(
        "ck_payroll_payment_amount_account_non_negative",
        "payroll_payment",
        type_="check",
    )
    op.drop_constraint(
        "ck_payroll_payment_amount_cash_non_negative",
        "payroll_payment",
        type_="check",
    )
    op.drop_constraint("ck_payroll_payment_method", "payroll_payment", type_="check")
    op.execute("delete from payroll_payment where status <> 'paid'")
    op.execute("update payroll_payment set paid_at = current_date where paid_at is null")
    op.execute("update payroll_payment set method = 'other' where method is null")
    op.alter_column("payroll_payment", "method", existing_type=sa.String(length=32), nullable=False)
    op.alter_column("payroll_payment", "paid_at", existing_type=sa.Date(), nullable=False)
    op.create_check_constraint(
        "ck_payroll_payment_method",
        "payroll_payment",
        "method in ('business_card', 'cash', 'transfer', 'other')",
    )
    op.drop_column("payroll_payment", "overpaid_amount")
    op.drop_column("payroll_payment", "draft_synced_at")
    op.drop_column("payroll_payment", "draft_status")
    op.drop_column("payroll_payment", "draft_document_id")
    op.drop_column("payroll_payment", "status")
    op.drop_column("payroll_payment", "amount_account")
    op.drop_column("payroll_payment", "amount_cash")


def _upsert_permissions() -> None:
    permission_table = sa.table(
        "permission",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("code", sa.String()),
        sa.column("module", sa.String()),
        sa.column("description", sa.String()),
    )
    rows = [
        {"id": uuid.uuid4(), "code": code, "module": module, "description": description}
        for code, module, description in PERMISSION_CATALOG
    ]
    insert_stmt = postgresql.insert(permission_table).values(rows)
    op.get_bind().execute(
        insert_stmt.on_conflict_do_update(
            index_elements=["code"],
            set_={
                "module": insert_stmt.excluded.module,
                "description": insert_stmt.excluded.description,
            },
        )
    )


def _grant_permissions(role_codes: tuple[str, ...], permission_codes: tuple[str, ...]) -> None:
    if not role_codes or not permission_codes:
        return
    op.execute(
        f"""
        insert into role_permission (role_id, permission_id)
        select role.id, permission.id
        from role
        cross join permission
        where role.code in ({_sql_values(role_codes)})
          and permission.code in ({_sql_values(permission_codes)})
        on conflict (role_id, permission_id) do nothing
        """
    )


def _sql_values(values: tuple[str, ...]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)

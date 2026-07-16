"""Периоды услуг поставщиков, журнал P&L и право корректировки признанного периода.

Revision ID: 0187_supplier_service_periods
Revises: 0186_payroll_reserve_run_link
Create Date: 2026-07-13
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.auth.permissions import PERMISSION_CATALOG

revision = "0190_supplier_service_periods"
down_revision = "0189_payroll_draft_deleted"
branch_labels = None
depends_on = None

NEW_CODES = ("accounting.service_periods.correct_recognized",)
GRANT_ROLES = ("owner", "admin")


def upgrade() -> None:
    op.add_column(
        "counterparty_payable_profile",
        sa.Column(
            "service_period_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "counterparty_payable_profile",
        sa.Column("default_service_period_offset_months", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_payable_profile_service_period_offset",
        "counterparty_payable_profile",
        "default_service_period_offset_months is null "
        "or default_service_period_offset_months between -12 and 12",
    )

    op.add_column(
        "counterparty_payment_draft",
        sa.Column("service_period_start", sa.Date(), nullable=True),
    )
    op.add_column(
        "counterparty_payment_draft",
        sa.Column("service_period_end", sa.Date(), nullable=True),
    )
    op.add_column(
        "expense_draft_line",
        sa.Column("service_period_start", sa.Date(), nullable=True),
    )
    op.add_column(
        "expense_draft_line",
        sa.Column("service_period_end", sa.Date(), nullable=True),
    )
    op.add_column(
        "expense_draft_line",
        sa.Column("service_period_source", sa.String(length=24), nullable=True),
    )

    op.add_column("supplier_invoice", sa.Column("service_period_start", sa.Date(), nullable=True))
    op.add_column("supplier_invoice", sa.Column("service_period_end", sa.Date(), nullable=True))
    op.add_column(
        "supplier_invoice",
        sa.Column("service_period_source", sa.String(length=24), nullable=True),
    )
    op.add_column(
        "supplier_invoice",
        sa.Column(
            "service_period_status",
            sa.String(length=24),
            nullable=False,
            server_default="not_required",
        ),
    )
    op.add_column(
        "supplier_invoice",
        sa.Column("service_period_confidence", sa.Numeric(4, 3), nullable=True),
    )

    op.add_column(
        "supplier_prepayment",
        sa.Column("service_period_start", sa.Date(), nullable=True),
    )
    op.add_column(
        "supplier_prepayment",
        sa.Column("service_period_end", sa.Date(), nullable=True),
    )
    op.add_column(
        "supplier_prepayment",
        sa.Column(
            "service_period_status",
            sa.String(length=24),
            nullable=False,
            server_default="missing",
        ),
    )

    op.create_table(
        "supplier_expense_accrual",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("counterparty_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("invoice_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("expense_draft_line_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("payment_draft_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("article_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("service_period_start", sa.Date(), nullable=False),
        sa.Column("service_period_end", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="scheduled"),
        sa.Column("recognition_month", sa.Date(), nullable=True),
        sa.Column("recognized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status in ('scheduled', 'recognized', 'cancelled')",
            name="ck_supplier_expense_accrual_status",
        ),
        sa.CheckConstraint(
            "service_period_end >= service_period_start",
            name="ck_supplier_expense_accrual_period",
        ),
        sa.ForeignKeyConstraint(["article_id"], ["dds_articles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["counterparty_id"], ["counterparty.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["expense_draft_line_id"], ["expense_draft_line.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["invoice_id"], ["supplier_invoice.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["payment_draft_id"], ["counterparty_payment_draft.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("expense_draft_line_id", name="uq_supplier_expense_accrual_line"),
        sa.UniqueConstraint("invoice_id", name="uq_supplier_expense_accrual_invoice"),
    )
    op.create_index(
        "ix_supplier_expense_accrual_counterparty",
        "supplier_expense_accrual",
        ["counterparty_id"],
    )
    op.create_index(
        "ix_supplier_expense_accrual_due",
        "supplier_expense_accrual",
        ["status", "service_period_end"],
    )

    op.create_table(
        "supplier_service_period_change",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("accrual_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("old_period_start", sa.Date(), nullable=False),
        sa.Column("old_period_end", sa.Date(), nullable=False),
        sa.Column("new_period_start", sa.Date(), nullable=False),
        sa.Column("new_period_end", sa.Date(), nullable=False),
        sa.Column("old_recognition_month", sa.Date(), nullable=True),
        sa.Column("new_recognition_month", sa.Date(), nullable=True),
        sa.Column("actor_user_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["accrual_id"], ["supplier_expense_accrual.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_supplier_service_period_change_accrual",
        "supplier_service_period_change",
        ["accrual_id"],
    )

    _upsert_permissions()
    _grant_permissions(GRANT_ROLES, NEW_CODES)


def downgrade() -> None:
    codes = _sql_values(NEW_CODES)
    op.execute(
        f"delete from role_permission_event using permission "
        f"where role_permission_event.permission_id = permission.id "
        f"and permission.code in ({codes})"
    )
    op.execute(
        f"delete from role_permission using permission "
        f"where role_permission.permission_id = permission.id "
        f"and permission.code in ({codes})"
    )
    op.execute(f"delete from permission where code in ({codes})")

    op.drop_index(
        "ix_supplier_service_period_change_accrual",
        table_name="supplier_service_period_change",
    )
    op.drop_table("supplier_service_period_change")
    op.drop_index("ix_supplier_expense_accrual_due", table_name="supplier_expense_accrual")
    op.drop_index("ix_supplier_expense_accrual_counterparty", table_name="supplier_expense_accrual")
    op.drop_table("supplier_expense_accrual")

    for column in ("service_period_status", "service_period_end", "service_period_start"):
        op.drop_column("supplier_prepayment", column)
    for column in (
        "service_period_confidence",
        "service_period_status",
        "service_period_source",
        "service_period_end",
        "service_period_start",
    ):
        op.drop_column("supplier_invoice", column)
    for column in ("service_period_source", "service_period_end", "service_period_start"):
        op.drop_column("expense_draft_line", column)
    for column in ("service_period_end", "service_period_start"):
        op.drop_column("counterparty_payment_draft", column)
    op.drop_constraint(
        "ck_payable_profile_service_period_offset",
        "counterparty_payable_profile",
        type_="check",
    )
    for column in (
        "default_service_period_offset_months",
        "service_period_required",
    ):
        op.drop_column("counterparty_payable_profile", column)


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
    op.execute(
        f"""
        insert into role_permission (role_id, permission_id)
        select role.id, permission.id
        from role cross join permission
        where role.code in ({_sql_values(role_codes)})
          and permission.code in ({_sql_values(permission_codes)})
        on conflict (role_id, permission_id) do nothing
        """
    )


def _sql_values(values: tuple[str, ...]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)

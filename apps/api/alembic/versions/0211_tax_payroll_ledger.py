"""Налоги: канон помесячной раскладки зарплаты (сальдо-оборотная ведомость).

Таблица ``tax_payroll_ledger`` хранит помесячные начисления/удержания по сотруднику из
оборотки бухгалтера. Питает разнос зарплатного ЕНП (НДФЛ+взносы как эталон) и монитор
зарплатного критерия льготы по НДС. Плюс новый тип входящего документа ``turnover_statement``.

Revision ID: 0211_tax_payroll_ledger
Revises: 0210_tax_permissions
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0211_tax_payroll_ledger"
down_revision = "0210_tax_permissions"
branch_labels = None
depends_on = None

_TYPE_CK = "ck_tax_document_intake_type"
_TYPE_CK_OLD = "document_type in ('payment_order', 'payroll_statement', 'unknown')"
_TYPE_CK_NEW = (
    "document_type in ('payment_order', 'payroll_statement', "
    "'turnover_statement', 'unknown')"
)


def upgrade() -> None:
    op.create_table(
        "tax_payroll_ledger",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("tab_number", sa.String(length=16), nullable=True),
        sa.Column("employee", sa.String(length=200), nullable=False),
        sa.Column("oklad", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("days", sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column("accrued", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("ndfl", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("advance", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("contributions", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("injury", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("deduction", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("to_pay", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("intake_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tax_payroll_ledger"),
        sa.UniqueConstraint("year", "month", "tab_number", name="uq_tax_payroll_ledger_slot"),
        sa.CheckConstraint("month between 1 and 12", name="ck_tax_payroll_ledger_month"),
        sa.CheckConstraint(
            "accrued is null or accrued >= 0", name="ck_tax_payroll_ledger_accrued"
        ),
        sa.ForeignKeyConstraint(
            ["intake_id"],
            ["tax_document_intake.id"],
            name="fk_tax_payroll_ledger_intake",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_tax_payroll_ledger_period", "tax_payroll_ledger", ["year", "month"])

    # Расширяем перечень типов входящих документов новым значением.
    op.drop_constraint(_TYPE_CK, "tax_document_intake", type_="check")
    op.create_check_constraint(_TYPE_CK, "tax_document_intake", _TYPE_CK_NEW)


def downgrade() -> None:
    op.drop_constraint(_TYPE_CK, "tax_document_intake", type_="check")
    op.create_check_constraint(_TYPE_CK, "tax_document_intake", _TYPE_CK_OLD)

    op.drop_index("ix_tax_payroll_ledger_period", table_name="tax_payroll_ledger")
    op.drop_table("tax_payroll_ledger")

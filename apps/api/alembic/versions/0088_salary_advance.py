"""salary advance / loan ledger

Авансы и займы сотрудникам payroll-контура. Единая сущность с дискриминаторами
`role` (разводит возврат по производственной/админской ведомости) и `kind`
(advance|loan). Аванс гасится разом в ближайшей ведомости, заём — в рассрочку
равными долями за N периодов. Возврат нетится в PayrollLine.deduction;
salary_advance_recovery хранит факты удержания по ведомостям.

Revision ID: 0088_salary_advance
Revises: 0087_admin_payroll_permissions
Create Date: 2026-06-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0088_salary_advance"
down_revision = "0087_admin_payroll_permissions"
branch_labels = None
depends_on = None

_ADVANCE = "salary_advance"
_RECOVERY = "salary_advance_recovery"


def upgrade() -> None:
    op.create_table(
        _ADVANCE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("employee_id", UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=160), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="advance"),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("per_installment_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("installments_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("recovered_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="issued"),
        sa.Column("issued_on", sa.Date(), nullable=False),
        sa.Column("payout_method", sa.String(length=32), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_label", sa.String(length=160), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.CheckConstraint("kind in ('advance', 'loan')", name="ck_salary_advance_kind"),
        sa.CheckConstraint("amount > 0", name="ck_salary_advance_amount_positive"),
        sa.CheckConstraint(
            "per_installment_amount > 0",
            name="ck_salary_advance_per_installment_positive",
        ),
        sa.CheckConstraint(
            "installments_count >= 1", name="ck_salary_advance_installments_positive"
        ),
        sa.CheckConstraint(
            "recovered_amount >= 0", name="ck_salary_advance_recovered_non_negative"
        ),
        sa.CheckConstraint(
            "recovered_amount <= amount", name="ck_salary_advance_recovered_le_amount"
        ),
        sa.CheckConstraint(
            "status in ('issued', 'recovered', 'written_off', 'cancelled')",
            name="ck_salary_advance_status",
        ),
        sa.CheckConstraint(
            "payout_method is null or payout_method in "
            "('business_card', 'cash', 'transfer', 'other')",
            name="ck_salary_advance_payout_method",
        ),
    )
    op.create_index(
        "ix_salary_advance_employee_status", _ADVANCE, ["employee_id", "status"]
    )
    op.create_index("ix_salary_advance_role_status", _ADVANCE, ["role", "status"])
    op.create_index("ix_salary_advance_issued_on", _ADVANCE, ["issued_on"])

    op.create_table(
        _RECOVERY,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("advance_id", UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["advance_id"], ["salary_advance.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["payroll_run.id"], ondelete="SET NULL"),
        sa.CheckConstraint("amount > 0", name="ck_salary_advance_recovery_amount_positive"),
    )
    op.create_index("ix_salary_advance_recovery_advance", _RECOVERY, ["advance_id"])
    op.create_index("ix_salary_advance_recovery_run", _RECOVERY, ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_salary_advance_recovery_run", table_name=_RECOVERY)
    op.drop_index("ix_salary_advance_recovery_advance", table_name=_RECOVERY)
    op.drop_table(_RECOVERY)
    op.drop_index("ix_salary_advance_issued_on", table_name=_ADVANCE)
    op.drop_index("ix_salary_advance_role_status", table_name=_ADVANCE)
    op.drop_index("ix_salary_advance_employee_status", table_name=_ADVANCE)
    op.drop_table(_ADVANCE)

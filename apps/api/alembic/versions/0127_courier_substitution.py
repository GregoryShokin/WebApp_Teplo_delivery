"""Подмена плейсхолдер-курьеров: флаг is_courier_placeholder + таблица подмен.

«Курьер 1» — общая карточка iiko, под которой открывает смену курьер без собственной
карточки. Помечаем такие карточки флагом employee.is_courier_placeholder, а фактического
курьера за день фиксируем в courier_shift_substitution. Существующий «Курьер 1» помечаем
сразу (имя приходит из iiko и едино для dev/prod).

Revision ID: 0127_courier_substitution
Revises: 0126_counterparty_kassa_enabled
Create Date: 2026-06-19
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0127_courier_substitution"
down_revision = "0126_counterparty_kassa_enabled"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employee",
        sa.Column(
            "is_courier_placeholder",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="source=app_managed; общая карточка для смен курьеров без своей карточки iiko",
        ),
    )

    op.create_table(
        "courier_shift_substitution",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("placeholder_employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("real_employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["placeholder_employee_id"], ["employee.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["real_employee_id"], ["employee.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "placeholder_employee_id <> real_employee_id",
            name="ck_courier_shift_substitution_distinct",
        ),
        sa.UniqueConstraint(
            "work_date",
            "placeholder_employee_id",
            name="uq_courier_shift_substitution_date_placeholder",
        ),
    )
    op.create_index(
        "ix_courier_shift_substitution_real_date",
        "courier_shift_substitution",
        ["real_employee_id", "work_date"],
    )

    # Существующий рабочий плейсхолдер.
    op.execute("update employee set is_courier_placeholder = true where full_name = 'Курьер 1'")


def downgrade() -> None:
    op.drop_index(
        "ix_courier_shift_substitution_real_date",
        table_name="courier_shift_substitution",
    )
    op.drop_table("courier_shift_substitution")
    op.drop_column("employee", "is_courier_placeholder")

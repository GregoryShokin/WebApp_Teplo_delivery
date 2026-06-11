"""counterparty profile payment terms and manager contact

Доп. поля профиля поставщика: день месяца оплаты (платить до N-го числа) и контакт
менеджера поставщика (имя + телефон). Отсрочка от даты поставки уже есть
(payment_delay_days). Приоритет due-date: iiko dueDate > отсрочка > день месяца.

Revision ID: 0096_cp_profile_terms
Revises: 0095_cp_collection_source
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0096_cp_profile_terms"
down_revision = "0095_cp_collection_source"
branch_labels = None
depends_on = None

_TABLE = "counterparty_payable_profile"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("payment_due_day_of_month", sa.Integer(), nullable=True))
    op.add_column(_TABLE, sa.Column("manager_name", sa.String(length=160), nullable=True))
    op.add_column(_TABLE, sa.Column("manager_phone", sa.String(length=64), nullable=True))
    op.create_check_constraint(
        "ck_payable_profile_due_day",
        _TABLE,
        "payment_due_day_of_month is null "
        "or (payment_due_day_of_month >= 1 and payment_due_day_of_month <= 31)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_payable_profile_due_day", _TABLE, type_="check")
    op.drop_column(_TABLE, "manager_phone")
    op.drop_column(_TABLE, "manager_name")
    op.drop_column(_TABLE, "payment_due_day_of_month")

"""courier deposit created_by references user instead of employee

Депозитные операции курьеров теперь атрибутируются на залогиненного
пользователя (как премии/штрафы/Учёт смен), а не на сотрудника. Поэтому
FK `courier_deposit_transaction.created_by` переводится с `employee.id`
на `user.id`. На момент миграции таблица пустая (фича была заблокирована),
поэтому ремап данных не требуется.

Revision ID: 0078_courier_deposit_created_by_user
Revises: 0077_payroll_bank_draft
Create Date: 2026-06-09
"""

from __future__ import annotations

from alembic import op

revision = "0078_courier_deposit_by_user"
down_revision = "0077_payroll_bank_draft"
branch_labels = None
depends_on = None

_TABLE = "courier_deposit_transaction"
_EMPLOYEE_FK = "fk_courier_deposit_transaction_created_by_employee"
_USER_FK = "fk_courier_deposit_transaction_created_by_user"


def upgrade() -> None:
    op.drop_constraint(_EMPLOYEE_FK, _TABLE, type_="foreignkey")
    op.create_foreign_key(
        _USER_FK,
        _TABLE,
        "user",
        ["created_by"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(_USER_FK, _TABLE, type_="foreignkey")
    op.create_foreign_key(
        _EMPLOYEE_FK,
        _TABLE,
        "employee",
        ["created_by"],
        ["id"],
        ondelete="RESTRICT",
    )

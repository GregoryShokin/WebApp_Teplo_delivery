"""courier evaluation/schedule created_by references user instead of employee

Оценки и график курьеров теперь атрибутируются на залогиненного пользователя
(как депозиты), а не на сотрудника. FK `created_by` обеих таблиц переводится
с `employee.id` на `user.id`. Колонка делается nullable, а существующие
значения, не являющиеся пользователями (legacy, ссылались на сотрудников),
обнуляются — у нас нет надёжного маппинга employee->user.

Revision ID: 0080_courier_actor_user
Revises: 0079_senior_courier_role
Create Date: 2026-06-09
"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0080_courier_actor_user"
down_revision = "0079_senior_courier_role"
branch_labels = None
depends_on = None

_TABLES = (
    ("courier_evaluation", "fk_courier_evaluation_created_by_employee", "fk_courier_evaluation_created_by_user"),
    (
        "courier_schedule_entry",
        "fk_courier_schedule_entry_created_by_employee",
        "fk_courier_schedule_entry_created_by_user",
    ),
)


def upgrade() -> None:
    for table, employee_fk, user_fk in _TABLES:
        op.drop_constraint(employee_fk, table, type_="foreignkey")
        op.alter_column(
            table,
            "created_by",
            existing_type=postgresql.UUID(as_uuid=True),
            nullable=True,
        )
        op.execute(
            f'UPDATE {table} SET created_by = NULL '
            f'WHERE created_by IS NOT NULL AND created_by NOT IN (SELECT id FROM "user")'
        )
        op.create_foreign_key(
            user_fk,
            table,
            "user",
            ["created_by"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    for table, employee_fk, user_fk in _TABLES:
        op.drop_constraint(user_fk, table, type_="foreignkey")
        op.create_foreign_key(
            employee_fk,
            table,
            "employee",
            ["created_by"],
            ["id"],
            ondelete="RESTRICT",
        )

"""add Senior Courier RBAC role

Новая назначаемая/редактируемая роль «Старший курьер» (senior_courier).
По умолчанию получает права просмотра курьеров; точный набор прав
(редактирование графика/оценок и т.д.) Собственник проставляет в
«Управлении доступом». Привязки к записи сотрудника роль не требует.

Revision ID: 0079_senior_courier_role
Revises: 0078_courier_deposit_by_user
Create Date: 2026-06-09
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0079_senior_courier_role"
down_revision = "0078_courier_deposit_by_user"
branch_labels = None
depends_on = None

ROLE_CODE = "senior_courier"
ROLE_NAME = "Старший курьер"

# Базовый набор по умолчанию — только просмотр курьеров. Права на
# редактирование Собственник добавляет вручную в «Управлении доступом».
DEFAULT_PERMISSION_CODES = (
    "couriers.list.read",
    "couriers.statistics.read",
    "couriers.schedule.read",
    "couriers.evaluations.read",
    "couriers.deposits.read",
)


def upgrade() -> None:
    role_table = sa.table(
        "role",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("code", sa.String()),
        sa.column("name", sa.String()),
    )
    op.get_bind().execute(
        postgresql.insert(role_table)
        .values([{"id": uuid.uuid4(), "code": ROLE_CODE, "name": ROLE_NAME}])
        .on_conflict_do_nothing(index_elements=["code"])
    )
    codes = ", ".join("'" + code.replace("'", "''") + "'" for code in DEFAULT_PERMISSION_CODES)
    op.execute(
        f"""
        insert into role_permission (role_id, permission_id)
        select role.id, permission.id
        from role
        cross join permission
        where role.code = '{ROLE_CODE}'
          and permission.code in ({codes})
        on conflict (role_id, permission_id) do nothing
        """
    )


def downgrade() -> None:
    op.execute(
        f"delete from user_role where role_id in (select id from role where code = '{ROLE_CODE}')"
    )
    op.execute(
        f"delete from role_permission where role_id in "
        f"(select id from role where code = '{ROLE_CODE}')"
    )
    op.execute(f"delete from role where code = '{ROLE_CODE}'")

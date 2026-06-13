"""Слияние ролей доступа с реестром должностей + Старший курьер как должность.

Что делает:
  - position: + participates_in_access (toggle участия в выдаче доступов),
    + access_role_code (связь должности с ролью-движком прав); убирает 4 флага
    допусков (они выводятся из архетипа в position_registry);
  - привязывает существующие должности к ролям доступа (Управляющий→manager,
    Менеджер→office_manager, Кассир→cashier, Системный администратор→admin);
  - заводит должность «Старший курьер» (archetype courier, роль senior_courier);
  - добавляет право couriers.senior.assign («Назначать старшего курьера») и
    грантит управленцам;
  - мигрирует курьеров с галочкой старшинства (is_senior) в должность
    «Старший курьер» и снимает галочку.

Revision ID: 0102_position_access_merge
Revises: 0101_merge_barter_position
Create Date: 2026-06-13
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.auth.permissions import PERMISSION_CATALOG

revision = "0102_position_access_merge"
down_revision = "0101_merge_barter_position"
branch_labels = None
depends_on = None

# должность реестра → код роли доступа (движок прав)
POSITION_ROLE_MAP = {
    "Управляющий": "manager",
    "Менеджер": "office_manager",
    "Кассир": "cashier",
    "Системный администратор": "admin",
}
SENIOR_COURIER_POSITION = "Старший курьер"
SENIOR_COURIER_ROLE = "senior_courier"
NEW_PERMISSION_CODE = "couriers.senior.assign"
SENIOR_ASSIGN_GRANT_ROLES = ("owner", "admin", "manager", "office_manager")
DROPPED_FLAG_COLUMNS = (
    "gets_vacation",
    "in_schedule",
    "gets_seniority_premium",
    "can_substitute",
)


def upgrade() -> None:
    op.add_column(
        "position",
        sa.Column(
            "participates_in_access",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column("position", sa.Column("access_role_code", sa.String(length=64), nullable=True))

    for position_name, role_code in POSITION_ROLE_MAP.items():
        op.execute(
            f"""
            update position
            set participates_in_access = true, access_role_code = '{role_code}'
            where name = '{position_name}'
            """
        )

    for column in DROPPED_FLAG_COLUMNS:
        op.drop_column("position", column)

    _seed_senior_courier()
    _upsert_permissions()
    _grant_permissions(SENIOR_ASSIGN_GRANT_ROLES, (NEW_PERMISSION_CODE,))
    _migrate_senior_couriers()


def downgrade() -> None:
    for column in DROPPED_FLAG_COLUMNS:
        op.add_column(
            "position",
            sa.Column(column, sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    op.execute(
        f"delete from position where name = '{SENIOR_COURIER_POSITION}'"
    )
    op.drop_column("position", "access_role_code")
    op.drop_column("position", "participates_in_access")

    codes = _sql_values((NEW_PERMISSION_CODE,))
    op.execute(
        f"""
        delete from role_permission_event
        using permission
        where role_permission_event.permission_id = permission.id
          and permission.code in ({codes})
        """
    )
    op.execute(
        f"""
        delete from role_permission
        using permission
        where role_permission.permission_id = permission.id
          and permission.code in ({codes})
        """
    )
    op.execute(f"delete from permission where code in ({codes})")


def _seed_senior_courier() -> None:
    position_table = sa.table(
        "position",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("name", sa.String()),
        sa.column("archetype", sa.String()),
        sa.column("permission_group", sa.String()),
        sa.column("status", sa.String()),
        sa.column("schedule_type", sa.String()),
        sa.column("participates_in_access", sa.Boolean()),
        sa.column("access_role_code", sa.String()),
    )
    stmt = postgresql.insert(position_table).values(
        [
            {
                "id": uuid.uuid4(),
                "name": SENIOR_COURIER_POSITION,
                "archetype": "courier",
                "permission_group": "couriers",
                "status": "active",
                "schedule_type": "SESSION",
                "participates_in_access": True,
                "access_role_code": SENIOR_COURIER_ROLE,
            }
        ]
    )
    op.get_bind().execute(stmt.on_conflict_do_nothing(index_elements=["name"]))


def _migrate_senior_couriers() -> None:
    # Курьеры со старой галочкой старшинства → должность «Старший курьер».
    op.execute(
        f"""
        update employee_position_assignment
        set position = '{SENIOR_COURIER_POSITION}'
        where position = 'Курьер' and effective_to is null
          and employee_id in (select id from employee where is_senior = true)
        """
    )
    op.execute(
        f"""
        update employee set is_senior = false
        where is_senior = true and id in (
            select employee_id from employee_position_assignment
            where position = '{SENIOR_COURIER_POSITION}' and effective_to is null
        )
        """
    )


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

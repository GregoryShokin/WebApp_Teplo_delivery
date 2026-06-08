"""add granular RBAC permissions

Revision ID: 0061_rbac_permissions
Revises: 0060_courier_shift_categories
Create Date: 2026-06-05
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0061_rbac_permissions"
down_revision = "0060_courier_shift_categories"
branch_labels = None
depends_on = None

ADDED_ROLES = (
    ("office_manager", "Менеджер"),
    ("cashier", "Кассир"),
)

FULL_ACCESS_ROLE_CODES = ("owner", "admin")

PERMISSIONS = (
    ("dds.read", "dds", "Просмотр ДДС"),
    ("dds.classify", "dds", "Классификация операций ДДС"),
    ("dds.write", "dds", "Редактирование ДДС"),
    ("dds.rules.write", "dds", "Управление правилами классификации"),
    ("payroll.read", "payroll", "Просмотр зарплаты"),
    ("payroll.runs.write", "payroll", "Расчёт зарплаты"),
    ("payroll.runs.finalize", "payroll", "Финализация расчёта зарплаты"),
    ("payroll.adjustments.write", "payroll", "Корректировки зарплаты"),
    ("schedule.read", "schedule", "Просмотр графика смен"),
    ("schedule.write", "schedule", "Редактирование графика смен"),
    ("shifts.read", "shifts", "Просмотр учёта смен и выручки"),
    ("shifts.write", "shifts", "Ввод учёта смен и выручки"),
    ("inventory.read", "inventory", "Просмотр инвентаризаций"),
    ("inventory.write", "inventory", "Редактирование инвентаризаций"),
    ("inventory.audits.write", "inventory", "Проведение инвентаризаций"),
    ("staff.read", "staff", "Просмотр Штата"),
    ("staff.write", "staff", "Редактирование Штата"),
    ("staff.dismiss", "staff", "Увольнение сотрудников"),
    ("staff.reinstate", "staff", "Восстановление сотрудников"),
    ("couriers.read", "couriers", "Просмотр курьеров"),
    ("couriers.schedule.write", "couriers", "График курьеров"),
    ("couriers.assess.write", "couriers", "Оценки курьеров"),
    ("couriers.deposits.write", "couriers", "Депозиты курьеров"),
    ("deposits.read", "deposits", "Просмотр депозитов"),
    ("deposits.write", "deposits", "Редактирование депозитов"),
    ("vacations.read", "vacations", "Просмотр отпусков"),
    ("vacations.write", "vacations", "Редактирование отпусков"),
    ("accumulation_fund.read", "accumulation_fund", "Просмотр фонда накопления"),
    ("accumulation_fund.write", "accumulation_fund", "Редактирование фонда накопления"),
    ("settings.read", "settings", "Просмотр настроек"),
    ("settings.write", "settings", "Редактирование настроек"),
    ("access_control.users.write", "access_control", "Управление пользователями"),
    ("access_control.roles.write", "access_control", "Управление правами должностей"),
)


def upgrade() -> None:
    op.create_table(
        "permission",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("code", sa.String(length=96), nullable=False),
        sa.Column("module", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.UniqueConstraint("code", name="uq_permission_code"),
    )
    op.create_table(
        "role_permission",
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permission.id"],
            name="fk_role_permission_permission_id_permission",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["role.id"],
            name="fk_role_permission_role_id_role",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("role_id", "permission_id", name="pk_role_permission"),
    )

    _seed_roles()
    _seed_permissions()
    _grant_full_access_roles()


def downgrade() -> None:
    op.drop_table("role_permission")
    op.drop_table("permission")
    op.execute(f"delete from role where code in ({_sql_values(_added_role_codes())})")


def _seed_roles() -> None:
    role_table = sa.table(
        "role",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("code", sa.String()),
        sa.column("name", sa.String()),
    )
    rows = [{"id": uuid.uuid4(), "code": code, "name": name} for code, name in ADDED_ROLES]
    op.get_bind().execute(
        postgresql.insert(role_table).values(rows).on_conflict_do_nothing(index_elements=["code"])
    )


def _seed_permissions() -> None:
    permission_table = sa.table(
        "permission",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("code", sa.String()),
        sa.column("module", sa.String()),
        sa.column("description", sa.String()),
    )
    rows = [
        {
            "id": uuid.uuid4(),
            "code": code,
            "module": module,
            "description": description,
        }
        for code, module, description in PERMISSIONS
    ]
    op.get_bind().execute(
        postgresql.insert(permission_table)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["code"])
    )


def _grant_full_access_roles() -> None:
    op.execute(
        f"""
            insert into role_permission (role_id, permission_id)
            select role.id, permission.id
            from role
            cross join permission
            where role.code in ({_sql_values(FULL_ACCESS_ROLE_CODES)})
              and permission.code in ({_sql_values(_permission_codes())})
            on conflict (role_id, permission_id) do nothing
            """
    )


def _added_role_codes() -> tuple[str, ...]:
    return tuple(code for code, _name in ADDED_ROLES)


def _permission_codes() -> tuple[str, ...]:
    return tuple(code for code, _module, _description in PERMISSIONS)


def _sql_values(values: tuple[str, ...]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)

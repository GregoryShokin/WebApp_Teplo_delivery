"""Реестр должностей: таблицы position / position_change_event + сид канона + права.

Сид воспроизводит прежний захардкоженный канон 1-в-1 (архетипы оплаты, группы
прав, допуски), чтобы поведение зарплаты и доступов не изменилось.

Права: settings.positions.read / settings.positions.edit.
Гранты: read — owner/admin/manager/office_manager; edit — owner/admin/office_manager
(по образцу settings.general.*).

Revision ID: 0100_position_registry
Revises: 0099_cp_relationship_direction
Create Date: 2026-06-12
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.auth.permissions import PERMISSION_CATALOG

revision = "0100_position_registry"
down_revision = "0099_cp_relationship_direction"
branch_labels = None
depends_on = None

NEW_CODES = ("settings.positions.read", "settings.positions.edit")
READ_GRANT_ROLES = ("owner", "admin", "manager", "office_manager")
EDIT_GRANT_ROLES = ("owner", "admin", "office_manager")

# Канон: name, archetype, permission_group, gets_vacation, in_schedule,
# gets_seniority_premium, can_substitute, schedule_type
SEED_POSITIONS = (
    ("Повар", "production_percent", "cooks", True, True, True, True, "SESSION"),
    ("Кассир", "production_percent", "cashiers", True, True, True, True, "SESSION"),
    ("Управляющий", "okladnik", "administration", False, False, False, False, "FIXED"),
    ("Менеджер", "okladnik", "administration", False, False, False, False, "SESSION"),
    (
        "Системный администратор",
        "okladnik",
        "administration",
        False,
        False,
        False,
        False,
        "FIXED",
    ),
    ("Уборщица", "okladnik", "auxiliary", False, False, False, False, "FIXED"),
    ("Посудомойка", "shift_pool", "auxiliary", False, False, False, False, "SESSION"),
    ("Курьер", "courier", "couriers", False, False, False, False, "SESSION"),
)


def upgrade() -> None:
    op.create_table(
        "position",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=160), nullable=False, unique=True),
        sa.Column("iiko_role_id", sa.String(length=64), nullable=True),
        sa.Column("iiko_role_code", sa.String(length=64), nullable=True),
        sa.Column("archetype", sa.String(length=32), nullable=False),
        sa.Column("permission_group", sa.String(length=32), nullable=False),
        sa.Column("gets_vacation", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("in_schedule", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "gets_seniority_premium", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("can_substitute", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column(
            "schedule_type", sa.String(length=16), nullable=False, server_default="SESSION"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "archetype in ('okladnik', 'production_percent', 'shift_pool', 'courier', 'none')",
            name="ck_position_archetype",
        ),
        sa.CheckConstraint(
            "permission_group in "
            "('administration', 'cooks', 'cashiers', 'auxiliary', 'couriers', 'none')",
            name="ck_position_permission_group",
        ),
        sa.CheckConstraint("status in ('active', 'excluded')", name="ck_position_status"),
        sa.CheckConstraint(
            "schedule_type in ('SESSION', 'FIXED', 'HOURS')",
            name="ck_position_schedule_type",
        ),
    )
    op.create_table(
        "position_change_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "position_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("position.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("position_name", sa.String(length=160), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("before_value", postgresql.JSONB(), nullable=True),
        sa.Column("after_value", postgresql.JSONB(), nullable=True),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    _seed_positions()
    _upsert_permissions()
    _grant_permissions(READ_GRANT_ROLES, ("settings.positions.read",))
    _grant_permissions(EDIT_GRANT_ROLES, ("settings.positions.edit",))


def downgrade() -> None:
    op.drop_table("position_change_event")
    op.drop_table("position")
    codes = _sql_values(NEW_CODES)
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


def _seed_positions() -> None:
    position_table = sa.table(
        "position",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("name", sa.String()),
        sa.column("archetype", sa.String()),
        sa.column("permission_group", sa.String()),
        sa.column("gets_vacation", sa.Boolean()),
        sa.column("in_schedule", sa.Boolean()),
        sa.column("gets_seniority_premium", sa.Boolean()),
        sa.column("can_substitute", sa.Boolean()),
        sa.column("status", sa.String()),
        sa.column("schedule_type", sa.String()),
    )
    rows = [
        {
            "id": uuid.uuid4(),
            "name": name,
            "archetype": archetype,
            "permission_group": permission_group,
            "gets_vacation": gets_vacation,
            "in_schedule": in_schedule,
            "gets_seniority_premium": gets_seniority_premium,
            "can_substitute": can_substitute,
            "status": "active",
            "schedule_type": schedule_type,
        }
        for (
            name,
            archetype,
            permission_group,
            gets_vacation,
            in_schedule,
            gets_seniority_premium,
            can_substitute,
            schedule_type,
        ) in SEED_POSITIONS
    ]
    op.bulk_insert(position_table, rows)


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

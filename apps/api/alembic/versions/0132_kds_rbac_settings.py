"""КДС-лайт: права, роль терминала упаковки, общий kiosk-пользователь, настройки.

- Гранулярные права ``kds.queue.read/write`` (планшет упаковки) и ``kitchen.speed.read``
  (дашборд скорости) — по образцу ``0119_kassa_permissions``.
- Узкая роль ``kitchen`` (только очередь упаковки) + общий kiosk-пользователь
  ``kitchen@teplo.local`` (пароль по умолчанию ``Teplo-KDS-2026`` — сменить после go-live).
- Раздача: очередь — owner/admin/manager/kitchen; дашборд — owner/admin/manager.
- Настройки категории ``kitchen``: пороги самонуджа, горизонт предзаказа,
  допуск опоздания горячих, organizationId iikoCloud (заполняется авто-discovery).

Revision ID: 0132_kds_rbac_settings
Revises: 0131_kds_tables
Create Date: 2026-06-21
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.auth.permissions import PERMISSION_CATALOG
from app.core.security import hash_password

revision = "0132_kds_rbac_settings"
down_revision = "0131_kds_tables"
branch_labels = None
depends_on = None

NEW_CODES = ("kds.queue.read", "kds.queue.write", "kitchen.speed.read")
GRANT_QUEUE_ROLES = ("owner", "admin", "manager", "kitchen")
QUEUE_CODES = ("kds.queue.read", "kds.queue.write")
GRANT_DASHBOARD_ROLES = ("owner", "admin", "manager")
DASHBOARD_CODES = ("kitchen.speed.read",)

KITCHEN_ROLE_CODE = "kitchen"
KITCHEN_ROLE_NAME = "Кухня/Упаковка"
KIOSK_USER_ID = uuid.UUID("d5b6f0c1-1a2b-4c3d-9e4f-5a6b7c8d9e01")
KIOSK_USER_EMAIL = "kitchen@teplo.local"
KIOSK_USER_NAME = "Терминал упаковки"
KIOSK_USER_PASSWORD = "Teplo-KDS-2026"  # noqa: S105 — дефолт kiosk, сменить после go-live

SETTINGS = (
    {
        "id": uuid.UUID("c1a2b3c4-d5e6-4f70-8a90-0b1c2d3e4f50"),
        "key": "kds.ready_nudge_minutes",
        "value": 10,
        "value_type": "number",
        "display_name": "Самонудж «Готово» без «Ждёт курьера», мин",
        "description": "Подсветка/звук, если заказ висит в статусе «Готово» дольше порога.",
        "widget_type": "number",
        "unit": "мин",
    },
    {
        "id": uuid.UUID("c1a2b3c4-d5e6-4f70-8a90-0b1c2d3e4f51"),
        "key": "kds.waiting_courier_nudge_minutes",
        "value": 15,
        "value_type": "number",
        "display_name": "Самонудж «Ждёт курьера», мин",
        "description": "Подсветка/звук, если упакованный заказ ждёт курьера дольше порога.",
        "widget_type": "number",
        "unit": "мин",
    },
    {
        "id": uuid.UUID("c1a2b3c4-d5e6-4f70-8a90-0b1c2d3e4f52"),
        "key": "kds.preorder_horizon_minutes",
        "value": 120,
        "value_type": "number",
        "display_name": "Горизонт предзаказа, мин",
        "description": (
            "Заказ считается предзаказом, если (completeBefore − создание) больше порога. "
            "Предзаказы не завышают метрику скорости готовки."
        ),
        "widget_type": "number",
        "unit": "мин",
    },
    {
        "id": uuid.UUID("c1a2b3c4-d5e6-4f70-8a90-0b1c2d3e4f53"),
        "key": "kds.hot_late_tolerance_minutes",
        "value": 0,
        "value_type": "number",
        "display_name": "Допуск опоздания горячих позиций, мин",
        "description": "Сколько минут после completeBefore ещё не считается опозданием.",
        "widget_type": "number",
        "unit": "мин",
    },
    {
        "id": uuid.UUID("c1a2b3c4-d5e6-4f70-8a90-0b1c2d3e4f54"),
        "key": "kds.iiko_cloud_org_id",
        "value": "",
        "value_type": "string",
        "display_name": "iikoCloud organizationId",
        "description": (
            "ID организации iikoCloud для очереди КДС. Пусто — заполнится авто-discovery."
        ),
        "widget_type": "text",
        "unit": None,
    },
)


def upgrade() -> None:
    bind = op.get_bind()
    _upsert_permissions()
    _seed_role()
    _grant_permissions(GRANT_QUEUE_ROLES, QUEUE_CODES)
    _grant_permissions(GRANT_DASHBOARD_ROLES, DASHBOARD_CODES)
    _seed_kiosk_user(bind)
    _seed_settings(bind)


def downgrade() -> None:
    codes = _sql_values(NEW_CODES)
    setting_keys = _sql_values(tuple(str(setting["key"]) for setting in SETTINGS))
    op.execute(
        "delete from app_setting_history where setting_id in "
        f"(select id from app_setting where key in ({setting_keys}))"
    )
    op.execute(f"delete from app_setting where key in ({setting_keys})")
    op.execute(
        f"""
        delete from user_role
        using role
        where user_role.role_id = role.id and role.code = '{KITCHEN_ROLE_CODE}'
        """
    )
    op.execute(f"delete from \"user\" where id = '{KIOSK_USER_ID}'")
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
    op.execute(f"delete from role where code = '{KITCHEN_ROLE_CODE}'")


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


def _seed_role() -> None:
    role_table = sa.table(
        "role",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("code", sa.String()),
        sa.column("name", sa.String()),
    )
    stmt = postgresql.insert(role_table).values(
        id=uuid.uuid4(), code=KITCHEN_ROLE_CODE, name=KITCHEN_ROLE_NAME
    )
    op.get_bind().execute(stmt.on_conflict_do_nothing(index_elements=["code"]))


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


def _seed_kiosk_user(bind: sa.engine.Connection) -> None:
    user_table = sa.table(
        "user",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("email", sa.String()),
        sa.column("hashed_password", sa.String()),
        sa.column("full_name", sa.String()),
        sa.column("is_active", sa.Boolean()),
    )
    stmt = postgresql.insert(user_table).values(
        id=KIOSK_USER_ID,
        email=KIOSK_USER_EMAIL,
        hashed_password=hash_password(KIOSK_USER_PASSWORD),
        full_name=KIOSK_USER_NAME,
        is_active=True,
    )
    bind.execute(stmt.on_conflict_do_nothing(index_elements=["email"]))

    role_id = bind.scalar(
        sa.text("select id from role where code = :code"), {"code": KITCHEN_ROLE_CODE}
    )
    org_id = bind.scalar(sa.text("select id from organization order by created_at limit 1"))
    user_id = bind.scalar(
        sa.text('select id from "user" where email = :email'), {"email": KIOSK_USER_EMAIL}
    )
    if role_id is None or org_id is None or user_id is None:
        return
    op.execute(
        f"""
        insert into user_role (user_id, role_id, organization_id)
        values ('{user_id}', '{role_id}', '{org_id}')
        on conflict (user_id, role_id, organization_id) do nothing
        """
    )


def _seed_settings(bind: sa.engine.Connection) -> None:
    admin_user_id = bind.scalar(sa.text('select id from "user" order by created_at limit 1'))
    app_setting = sa.table(
        "app_setting",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("key", sa.String()),
        sa.column("value", postgresql.JSONB()),
        sa.column("value_type", sa.String()),
        sa.column("category", sa.String()),
        sa.column("display_name", sa.Text()),
        sa.column("description", sa.Text()),
        sa.column("widget_type", sa.Text()),
        sa.column("widget_options", postgresql.JSONB()),
        sa.column("unit", sa.Text()),
        sa.column("updated_by_user_id", postgresql.UUID(as_uuid=True)),
    )
    rows = [
        {
            "id": setting["id"],
            "key": setting["key"],
            "value": setting["value"],
            "value_type": setting["value_type"],
            "category": "kitchen",
            "display_name": setting["display_name"],
            "description": setting["description"],
            "widget_type": setting["widget_type"],
            "widget_options": None,
            "unit": setting["unit"],
            "updated_by_user_id": admin_user_id,
        }
        for setting in SETTINGS
    ]
    stmt = postgresql.insert(app_setting).values(rows)
    bind.execute(stmt.on_conflict_do_nothing(index_elements=["key"]))


def _sql_values(values: tuple[str, ...]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)

"""Права модуля «Касса»: смены, чеки, накладные из кассы, корректировки, справочники.

Заводит гранулярные права ``kassa.*`` и раздаёт их по образцу
``0117_invoice_permissions`` (``_upsert_permissions`` подхватывает новые коды из
``PERMISSION_CATALOG``). Раздача мирроет дефолты ролей из ``permissions.py``:

- базовый набор (смены read/sync, чеки read/create, накладные из кассы, справочники) —
  owner/admin/manager/office_manager/cashier;
- проводка наличного контура смены (``kassa.shifts.post``) — без кассира;
- создание корректировок кассы (``kassa.adjustments.create``) — точечное право: по
  умолчанию только у owner/admin (full-access), остальным выдаётся вручную в «Доступах».

Revision ID: 0119_kassa_permissions
Revises: 0118_kassa_cheque_and_shifts
Create Date: 2026-06-17
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.auth.permissions import PERMISSION_CATALOG

revision = "0119_kassa_permissions"
down_revision = "0118_kassa_cheque_and_shifts"
branch_labels = None
depends_on = None

NEW_CODES = (
    "kassa.shifts.read",
    "kassa.shifts.sync",
    "kassa.shifts.post",
    "kassa.cheques.read",
    "kassa.cheques.create",
    "kassa.invoices.create",
    "kassa.adjustments.create",
    "kassa.refs.read",
)

# Базовый набор — у всех рабочих ролей, включая кассира.
GRANT_ALL_ROLES = ("owner", "admin", "manager", "office_manager", "cashier")
CODES_FOR_ALL = (
    "kassa.shifts.read",
    "kassa.shifts.sync",
    "kassa.cheques.read",
    "kassa.cheques.create",
    "kassa.invoices.create",
    "kassa.refs.read",
)
# Проводка наличного контура — менеджерские роли, без кассира.
GRANT_MANAGER_ROLES = ("owner", "admin", "manager", "office_manager")
CODES_FOR_MANAGERS = ("kassa.shifts.post",)
# Корректировки кассы — точечно; по умолчанию только full-access роли.
GRANT_OWNER_ROLES = ("owner", "admin")
CODES_FOR_OWNERS = ("kassa.adjustments.create",)


def upgrade() -> None:
    _upsert_permissions()
    _grant_permissions(GRANT_ALL_ROLES, CODES_FOR_ALL)
    _grant_permissions(GRANT_MANAGER_ROLES, CODES_FOR_MANAGERS)
    _grant_permissions(GRANT_OWNER_ROLES, CODES_FOR_OWNERS)


def downgrade() -> None:
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

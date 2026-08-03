"""Права на отчёт ОПиУ: завести в каталоге и выдать ролям.

ПОЧЕМУ ОТДЕЛЬНАЯ МИГРАЦИЯ, А НЕ СТРОКА В КОДЕ. Каталог прав живёт в
``app/auth/permissions.py``, но проверка идёт по таблице: право, объявленное только в коде,
не существует для эндпоинта. Ровно на этом отчёт и отказал при первой проверке на стенде —
``Insufficient permission`` у администратора, у которого «должно быть всё».

Выдаём владельцу, администратору и финансовому менеджеру: ОПиУ — управленческий отчёт, и
смотреть его должен тот, кто принимает решения по деньгам. Кассиру и курьеру он не нужен и
показывает то, чего им видеть не следует.

Revision ID: 0254_pnl_permissions
Revises: 0253_pnl_iiko_facts
Create Date: 2026-08-03
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.auth.permissions import PERMISSION_CATALOG

revision = "0254_pnl_permissions"
down_revision = "0253_pnl_iiko_facts"
branch_labels = None
depends_on = None

NEW_CODES = ("reports.pnl.read", "reports.pnl.manual_input")
GRANT_ROLES = ("owner", "admin", "finance_manager")


def upgrade() -> None:
    _upsert_permissions()
    _grant_permissions(GRANT_ROLES, NEW_CODES)


def downgrade() -> None:
    codes = _sql_values(NEW_CODES)
    op.execute(
        f"delete from role_permission_event using permission "
        f"where role_permission_event.permission_id = permission.id "
        f"and permission.code in ({codes})"
    )
    op.execute(
        f"delete from role_permission using permission "
        f"where role_permission.permission_id = permission.id "
        f"and permission.code in ({codes})"
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
    op.execute(
        f"""
        insert into role_permission (role_id, permission_id)
        select role.id, permission.id
        from role cross join permission
        where role.code in ({_sql_values(role_codes)})
          and permission.code in ({_sql_values(permission_codes)})
        on conflict (role_id, permission_id) do nothing
        """
    )


def _sql_values(values: tuple[str, ...]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)

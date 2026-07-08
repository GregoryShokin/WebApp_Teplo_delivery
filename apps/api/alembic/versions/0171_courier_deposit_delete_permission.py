"""Право на удаление пополнений депозитов курьеров.

couriers.deposits.delete — разрешает физически удалить операцию пополнения депозита курьера
вместе со связанной приходной проводкой в ДДС. Без права история депозита доступна только на
просмотр. Возврат/списание удалять нельзя (у возврата уже прошло изъятие в iiko/банк-черновик —
их правят обратным пополнением). Грант по умолчанию только owner/admin; остальным (например,
конкретному управляющему) Собственник выдаёт точечно в «Доступах».

Revision ID: 0171_courier_deposit_delete
Revises: 0170_expense_line_counterparty
Create Date: 2026-07-08
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.auth.permissions import PERMISSION_CATALOG

revision = "0171_courier_deposit_delete"
down_revision = "0170_expense_line_counterparty"
branch_labels = None
depends_on = None

NEW_CODES = ("couriers.deposits.delete",)
GRANT_ROLES = ("owner", "admin")


def upgrade() -> None:
    _upsert_permissions()
    _grant_permissions(GRANT_ROLES, NEW_CODES)


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

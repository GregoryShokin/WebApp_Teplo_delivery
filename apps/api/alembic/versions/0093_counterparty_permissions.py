"""add counterparties module permissions

Права операционного модуля «Контрагенты»:
  - counterparties.read — смотреть контрагентов и счета к оплате;
  - counterparties.operate — отправлять платежи в банк и сводить с выписками;
  - counterparties.admin — редактировать реквизиты, реестр и справочники.

Гранты: read + operate — owner/admin/manager (Управляющий)/office_manager (Менеджер);
admin — только owner/admin; cashier — без доступа.

Revision ID: 0093_counterparty_permissions
Revises: 0092_counterparty_payables
Create Date: 2026-06-11
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.auth.permissions import PERMISSION_CATALOG

revision = "0093_counterparty_permissions"
down_revision = "0092_counterparty_payables"
branch_labels = None
depends_on = None

NEW_CODES = (
    "counterparties.read",
    "counterparties.operate",
    "counterparties.admin",
)
OPERATE_CODES = ("counterparties.read", "counterparties.operate")
OPERATE_GRANT_ROLES = ("owner", "admin", "manager", "office_manager")
ADMIN_CODES = ("counterparties.admin",)
ADMIN_GRANT_ROLES = ("owner", "admin")


def upgrade() -> None:
    _upsert_permissions()
    _grant_permissions(OPERATE_GRANT_ROLES, OPERATE_CODES)
    _grant_permissions(ADMIN_GRANT_ROLES, ADMIN_CODES)


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

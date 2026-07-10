"""Право на правку ОПЛАЧЕННОЙ накладной с отзывом и перевыставлением оплаты.

invoices.normal.edit_paid — разрешает исправить уже оплаченную обычную накладную (позиции и
сумму), когда поставщик прислал не ту накладную, а её успели провести и оплатить. При снижении
суммы излишек оплаты не «повисает» и не задваивает расход, а превращается в дебиторку поставщику
(SupplierPrepayment, «поставщик нам должен»), которую гасят будущие поставки. Обычная правка
(invoices.normal.edit) по-прежнему работает только с НЕоплаченной накладной. Грант по умолчанию
только owner/admin; управляющему Собственник выдаёт точечно в «Доступах».

Revision ID: 0178_invoice_edit_paid
Revises: 0177_advance_source_operation
Create Date: 2026-07-10
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.auth.permissions import PERMISSION_CATALOG

revision = "0178_invoice_edit_paid"
down_revision = "0177_advance_source_operation"
branch_labels = None
depends_on = None

NEW_CODES = ("invoices.normal.edit_paid",)
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

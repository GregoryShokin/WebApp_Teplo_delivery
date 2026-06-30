"""Ручная корректировка суммы недостачи позиции ревизии (костыль межревизионного пересорта).

Излишек предыдущей ревизии по товару иногда «всплывает» ложной недостачей в текущей
(напр. креветки +2000 на 01-е → −2000 на 07-е). До глобального авто-зачёта даём управляющему
и собственнику руками скорректировать сумму недостачи по позиции, не трогая исходные числа iiko:

- 4 поля на inventory_audit_item: знаковая корректировка + причина + кто/когда;
- новое право revisions.items.adjust, выдаётся owner / admin / manager (Управляющий).
  Менеджер (office_manager) НЕ получает — см. deny-list в permissions.py.

Revision ID: 0149_revision_item_adjustment
Revises: 0148_revision_comment_cleanup
Create Date: 2026-06-30
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.auth.permissions import PERMISSION_CATALOG

revision = "0149_revision_item_adjustment"
down_revision = "0148_revision_comment_cleanup"
branch_labels = None
depends_on = None

NEW_CODES = ("revisions.items.adjust",)
GRANT_ROLES = ("owner", "admin", "manager")


def upgrade() -> None:
    op.add_column(
        "inventory_audit_item",
        sa.Column("manual_shortage_adjustment", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "inventory_audit_item",
        sa.Column("manual_shortage_adjustment_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "inventory_audit_item",
        sa.Column(
            "manual_shortage_adjustment_by_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "inventory_audit_item",
        sa.Column(
            "manual_shortage_adjustment_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_inventory_audit_item_adjustment_user",
        "inventory_audit_item",
        "user",
        ["manual_shortage_adjustment_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )

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

    op.drop_constraint(
        "fk_inventory_audit_item_adjustment_user",
        "inventory_audit_item",
        type_="foreignkey",
    )
    op.drop_column("inventory_audit_item", "manual_shortage_adjustment_at")
    op.drop_column("inventory_audit_item", "manual_shortage_adjustment_by_user_id")
    op.drop_column("inventory_audit_item", "manual_shortage_adjustment_reason")
    op.drop_column("inventory_audit_item", "manual_shortage_adjustment")


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

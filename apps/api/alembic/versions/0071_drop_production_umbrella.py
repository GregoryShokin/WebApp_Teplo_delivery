"""drop staff.production umbrella, add per-group assign_roles_categories

Revision ID: 0071_drop_production_umbrella
Revises: 0070_drop_dup_inventory_perm
Create Date: 2026-06-06

Removes the staff.production.* umbrella permission (which redundantly covered
cooks+cashiers+auxiliary). Access is now granted strictly per staff group. The
only live umbrella-only action — assign_roles_categories — gets explicit
per-group codes (staff.cooks/cashiers.assign_roles_categories). assign_allowances
was never enforced and is dropped.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.auth.permissions import (
    ALL_PERMISSION_CODES,
    DEFAULT_ROLE_PERMISSIONS,
    FULL_ACCESS_ROLE_CODES,
    PERMISSION_CATALOG,
)

revision = "0071_drop_production_umbrella"
down_revision = "0070_drop_dup_inventory_perm"
branch_labels = None
depends_on = None

STALE_CODES = (
    "staff.production.read",
    "staff.production.edit",
    "staff.production.assign_roles_categories",
    "staff.production.assign_allowances",
    "staff.production.dismiss",
    "staff.production.reinstate",
    "staff.production.history.read",
)


def upgrade() -> None:
    _upsert_permissions()
    _drop_stale_permissions()
    _grant_permissions(tuple(FULL_ACCESS_ROLE_CODES), tuple(ALL_PERMISSION_CODES))
    for role_code, permission_codes in DEFAULT_ROLE_PERMISSIONS.items():
        _grant_permissions((role_code,), tuple(permission_codes))


def downgrade() -> None:
    # Seed-style migration; permission tables drop in 0061 on a full downgrade.
    pass


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


def _drop_stale_permissions() -> None:
    codes = _sql_values(STALE_CODES)
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

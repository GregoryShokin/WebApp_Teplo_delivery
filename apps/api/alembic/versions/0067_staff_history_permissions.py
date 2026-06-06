"""sync staff history permissions

Revision ID: 0067_staff_history_permissions
Revises: 0066_access_rights_matrix
Create Date: 2026-06-06
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

revision = "0067_staff_history_permissions"
down_revision = "0066_access_rights_matrix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _upsert_permissions()
    _grant_full_access_roles()
    _grant_default_role_permissions()


def downgrade() -> None:
    # Seed migrations are intentionally left in place on step-down. A full downgrade
    # to base drops the permission tables in 0061, including these rows.
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
        {
            "id": uuid.uuid4(),
            "code": code,
            "module": module,
            "description": description,
        }
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


def _grant_full_access_roles() -> None:
    _grant_permissions(tuple(FULL_ACCESS_ROLE_CODES), tuple(ALL_PERMISSION_CODES))


def _grant_default_role_permissions() -> None:
    for role_code, permission_codes in DEFAULT_ROLE_PERMISSIONS.items():
        _grant_permissions((role_code,), tuple(permission_codes))


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

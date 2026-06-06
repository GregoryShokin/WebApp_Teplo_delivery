"""drop duplicate accounting inventory/audit permissions

Revision ID: 0070_drop_dup_inventory_perm
Revises: 0069_source_data_tab_permissions
Create Date: 2026-06-06

Removes the dead duplicate permissions for the inventory-audit domain. The
canonical permissions are revisions.* (block «Ревизии»); these accounting.*
codes were never wired to any endpoint guard and only shadowed the revisions
block in the matrix.
"""

from __future__ import annotations

from alembic import op

revision = "0070_drop_dup_inventory_perm"
down_revision = "0069_source_data_tab_permissions"
branch_labels = None
depends_on = None

STALE_CODES = (
    "accounting.inventory.read",
    "accounting.inventory.conduct",
    "accounting.inventory.finalize",
    "accounting.inventory_directory.edit",
    "accounting.audits.read",
    "accounting.audit_penalties.add",
)


def upgrade() -> None:
    codes = _sql_values(STALE_CODES)
    # Remove dependent rows first to avoid FK violations, then the permissions.
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


def downgrade() -> None:
    # Seed-style migration. A full downgrade to base drops the permission tables
    # in 0061; these rows are re-created from the catalog by later seed steps.
    pass


def _sql_values(values: tuple[str, ...]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)

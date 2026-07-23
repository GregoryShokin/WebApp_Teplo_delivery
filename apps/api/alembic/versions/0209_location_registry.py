"""Реестр помещений: тип, идентификаторы iiko, сроки работы, права.

Помещение становится осью аналитики «где» (аренда, коммуналка, оборудование, выручка) и
единственным местом, где живут идентификаторы iiko. До этой миграции они были зашиты
константами в шести местах кода — `IIKO_ORGANIZATION_ID`, `CHERNIKOVA_DEPARTMENT_ID`,
`ACTIVE_DEPARTMENT_ID`, `IIKO_REVENUE_DEPARTMENT`, — то есть система физически не могла
обслуживать второй филиал.

Бэкфилл проставляет действующему помещению «Черникова» ровно те значения, что были в коде,
поэтому поведение до и после миграции совпадает. Закрытая «Гагарина» остаётся без
идентификаторов: её данных в iiko уже нет.

Revision ID: 0209_location_registry
Revises: 0208_merge_fund_iiko
Create Date: 2026-07-23
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.auth.permissions import PERMISSION_CATALOG

revision = "0209_location_registry"
down_revision = "0208_merge_fund_iiko"
branch_labels = None
depends_on = None

NEW_CODES = ("source.locations.read", "source.locations.edit")
GRANT_ROLES = ("owner", "admin")

# Значения из кода на момент миграции: iiko_cloud_client.IIKO_ORGANIZATION_ID и
# courier_sync.ACTIVE_DEPARTMENT_ID (он же CHERNIKOVA_DEPARTMENT_ID в трёх сервисах).
CHERNIKOVA_ORGANIZATION_ID = "5c7e51f9-93c6-450c-9299-440ac1c889e8"
CHERNIKOVA_DEPARTMENT_ID = "d8d4a22e-3abd-4f02-b82d-7d4712f32729"


def upgrade() -> None:
    op.add_column(
        "location",
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="point"),
    )
    op.add_column(
        "location", sa.Column("iiko_organization_id", sa.String(length=64), nullable=True)
    )
    op.add_column("location", sa.Column("iiko_department_id", sa.String(length=64), nullable=True))
    op.add_column(
        "location",
        sa.Column(
            "iiko_store_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column("location", sa.Column("opened_on", sa.Date(), nullable=True))
    op.add_column("location", sa.Column("closed_on", sa.Date(), nullable=True))
    op.add_column("location", sa.Column("note", sa.Text(), nullable=True))

    op.execute(
        f"""
        update location
        set iiko_organization_id = '{CHERNIKOVA_ORGANIZATION_ID}',
            iiko_department_id = '{CHERNIKOVA_DEPARTMENT_ID}'
        where status = 'active'
          and iiko_department_id is null
        """
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

    op.drop_column("location", "note")
    op.drop_column("location", "closed_on")
    op.drop_column("location", "opened_on")
    op.drop_column("location", "iiko_store_ids")
    op.drop_column("location", "iiko_department_id")
    op.drop_column("location", "iiko_organization_id")
    op.drop_column("location", "kind")


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

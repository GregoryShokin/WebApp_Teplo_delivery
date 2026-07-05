"""Касса: кассовый журнал и выплаты наличных по разрешённым статьям.

- ``dds_articles.kassa_enabled`` — статья доступна администратору в форме
  «Выплата из кассы» (аналог ``kassa_enabled`` у контрагентов). Курируется
  владельцем в каталоге статей; включается только для расходных (outflow)
  статей без собственных контуров выдачи (защита в API).
- ``cashflow_transactions.created_by_user_id`` — автор проводки. Заполняется
  ручными контурами (выплата из кассы); нужен для правила «править/удалять
  свою кассовую запись можно только в день создания».
- Права ``kassa.journal.read`` (вкладка «Кассовый журнал» — зеркало ДДС строго
  по ТК Черникова) и ``kassa.payouts.create`` (выплата из кассы + правка своих
  записей в день создания). Гранты — как у остальных kassa.*: владелец/админ,
  Управляющий, Офис-менеджер, Кассир.

Revision ID: 0159_kassa_journal_payout
Revises: 0158_informal_safe_payout
Create Date: 2026-07-05
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.auth.permissions import PERMISSION_CATALOG

revision = "0159_kassa_journal_payout"
down_revision = "0158_informal_safe_payout"
branch_labels = None
depends_on = None

NEW_CODES = ("kassa.journal.read", "kassa.payouts.create")
GRANT_ROLES = ("owner", "admin", "manager", "office_manager", "cashier")


def upgrade() -> None:
    op.add_column(
        "dds_articles",
        sa.Column(
            "kassa_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "cashflow_transactions",
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_cashflow_transactions_created_by_user_id",
        "cashflow_transactions",
        "user",
        ["created_by_user_id"],
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
        "fk_cashflow_transactions_created_by_user_id",
        "cashflow_transactions",
        type_="foreignkey",
    )
    op.drop_column("cashflow_transactions", "created_by_user_id")
    op.drop_column("dds_articles", "kassa_enabled")


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

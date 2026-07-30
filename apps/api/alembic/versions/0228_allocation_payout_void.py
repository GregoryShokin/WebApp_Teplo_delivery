"""Право на отмену ошибочной выплаты резерва — Сейф и касса.

Выплата резерва (``safe_payout`` с Сейфа, ``kassa_target_payout`` из кассы) была необратима:
кассовый журнал такие проводки не разбирает, у ленты ДДС правки нет вовсе. Ошибка суммой,
счётом или двойное проведение — расход висел вечно, а резерв числился оплаченным.

Право ``finance.allocation_payout.void`` намеренно отдельное от прав выдавать
(``finance.payout_channel.*``, ``kassa.payouts.create``): выдаёт кассовый администратор, а
откатывает уже отданные деньги тот, кто за них отвечает. Грант — owner/admin, по образцу
0121_kassa_shortage_penalty и 0219_freelancer_shift_void.

Схему миграция не трогает: отмена работает на существующих таблицах.

Revision ID: 0228_allocation_payout_void
Revises: 0227_capital_repair_floor
Create Date: 2026-07-31
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.auth.permissions import PERMISSION_CATALOG

revision = "0228_allocation_payout_void"
down_revision = "0227_capital_repair_floor"
branch_labels = None
depends_on = None

NEW_PERMISSION_CODE = "finance.allocation_payout.void"
VOID_GRANT_ROLES = ("owner", "admin")


def upgrade() -> None:
    _upsert_permissions()
    _grant_void_permission()


def downgrade() -> None:
    op.execute(
        f"""
        delete from role_permission
        using permission
        where role_permission.permission_id = permission.id
          and permission.code = '{NEW_PERMISSION_CODE}'
        """
    )
    op.execute(f"delete from permission where code = '{NEW_PERMISSION_CODE}'")


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


def _grant_void_permission() -> None:
    roles = ", ".join("'" + role.replace("'", "''") + "'" for role in VOID_GRANT_ROLES)
    op.execute(
        f"""
        insert into role_permission (role_id, permission_id)
        select role.id, permission.id
        from role
        cross join permission
        where role.code in ({roles})
          and permission.code = '{NEW_PERMISSION_CODE}'
        on conflict (role_id, permission_id) do nothing
        """
    )

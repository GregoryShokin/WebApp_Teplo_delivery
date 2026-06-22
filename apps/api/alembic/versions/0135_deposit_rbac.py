"""Гранулярные права на депозиты + права на счета выдачи.

Развод единых прав правки депозитов на отдельные операции и каналы выдачи, чтобы
Собственник раздавал доступ точечно (по образцу накладных, 0117):
- курьеры: couriers.deposits.{top_up,return,forfeit} вместо одного couriers.deposits.edit;
- производство: payroll.production_deposits.{payout,write_off} (отдельно от настройки .edit);
- каналы выдачи: finance.payout_channel.{cash_tk,safe,bank_draft} — нужны ДОПОЛНИТЕЛЬНО к
  операционному праву и для немедленной выдачи депозита, и для выбора наличного счёта/банк-
  черновика зарплаты.

Гранты НЕ сужают доступ: новые права + couriers.deposits.configure выдаются тем, кто платил
раньше — owner / admin / manager (Управляющий) / office_manager (Менеджер). Точную политику
(например, Менеджер — только Сейф+черновики, кто-то — только ТК) Собственник выставит в
разделе «Доступы».

Revision ID: 0135_deposit_rbac
Revises: 0134_deposit_payout_schedule
Create Date: 2026-06-22
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.auth.permissions import PERMISSION_CATALOG

revision = "0135_deposit_rbac"
down_revision = "0134_deposit_payout_schedule"
branch_labels = None
depends_on = None

NEW_CODES = (
    "couriers.deposits.top_up",
    "couriers.deposits.return",
    "couriers.deposits.forfeit",
    "couriers.deposits.configure",
    "payroll.production_deposits.payout",
    "payroll.production_deposits.write_off",
    "finance.payout_channel.cash_tk",
    "finance.payout_channel.safe",
    "finance.payout_channel.bank_draft",
)
GRANT_ROLES = ("owner", "admin", "manager", "office_manager")
# Эти права раньше были частью единого права и не должны исчезать у тех, кому раздавали.
ROLLBACK_CODES = (
    "couriers.deposits.top_up",
    "couriers.deposits.return",
    "couriers.deposits.forfeit",
    "payroll.production_deposits.payout",
    "payroll.production_deposits.write_off",
    "finance.payout_channel.cash_tk",
    "finance.payout_channel.safe",
    "finance.payout_channel.bank_draft",
)


def upgrade() -> None:
    _upsert_permissions()
    _grant_permissions(GRANT_ROLES, NEW_CODES)


def downgrade() -> None:
    # configure не удаляем (могло существовать до этой миграции). Удаляем только реально новые.
    codes = _sql_values(ROLLBACK_CODES)
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

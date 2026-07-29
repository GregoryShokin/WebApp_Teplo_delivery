"""Отмена ошибочной наличной выдачи внештатнику: статус ``void`` наконец выставляется.

Статус ``void`` объявлен ещё в 0165 («оплата аннулирована»), но за всё время не выставлялся
нигде: единственная запись в таблицу — создание строки ``paid_cash``. Админ, выдавший
наличные не тому человеку или не за ту смену, откатить операцию не мог ничем: расход в ДДС
висел вечно, а зачёт в ведомости считал смену оплаченной.

Что нужно контуру отмены сверх статуса:

1. ``attendance_entry_id`` становится NULLABLE. Отменённая строка отвязывается от явки —
   иначе она держала бы уникальность (повторно выдать ту же смену стало бы нельзя: строка
   ``void`` занимает ``uq_freelancer_shift_settlement_entry``) и саму явку под ``RESTRICT``
   из 0218 (перезагрузка явок периода либо падала бы на FK, либо обходила смену, навсегда
   заморозив её снимок). NULL в уникальном индексе Postgres не конфликтует, поэтому
   ограничение остаётся прежним и продолжает защищать от двойной выдачи действующих строк.
2. ``reversal_transaction_id`` — сторно-приход, которым отменена выдача ЗА ПРОШЛУЮ ДАТУ.
   Отмена в день выдачи проводку не сторнирует, а удаляет (канон кассовой правки: своя
   сегодняшняя запись стирается целиком), и тогда поле остаётся NULL.
3. ``voided_at`` / ``voided_by_user_id`` — кто и когда отменил.
4. Право ``kassa.freelancer_shift.void`` — отмена отделена от выдачи (``kassa.payouts.create``),
   грант только owner/admin (по образцу 0121_kassa_shortage_penalty).

Revision ID: 0219_freelancer_shift_void
Revises: 0218_attendance_open_shift
Create Date: 2026-07-29
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.auth.permissions import PERMISSION_CATALOG

revision = "0219_freelancer_shift_void"
down_revision = "0218_attendance_open_shift"
branch_labels = None
depends_on = None

NEW_PERMISSION_CODE = "kassa.freelancer_shift.void"
VOID_GRANT_ROLES = ("owner", "admin")


def upgrade() -> None:
    op.alter_column(
        "freelancer_shift_settlement",
        "attendance_entry_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.add_column(
        "freelancer_shift_settlement",
        sa.Column("reversal_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "freelancer_shift_settlement",
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "freelancer_shift_settlement",
        sa.Column("voided_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_freelancer_settlement_reversal_transaction",
        "freelancer_shift_settlement",
        "cashflow_transactions",
        ["reversal_transaction_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_freelancer_settlement_voided_by_user",
        "freelancer_shift_settlement",
        "user",
        ["voided_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )

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

    op.drop_constraint(
        "fk_freelancer_settlement_voided_by_user",
        "freelancer_shift_settlement",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_freelancer_settlement_reversal_transaction",
        "freelancer_shift_settlement",
        type_="foreignkey",
    )
    op.drop_column("freelancer_shift_settlement", "voided_by_user_id")
    op.drop_column("freelancer_shift_settlement", "voided_at")
    op.drop_column("freelancer_shift_settlement", "reversal_transaction_id")
    # Отвязанные от явки строки существуют ТОЛЬКО как след отмены. Вернуть им явку нечем
    # (её могло не остаться), а NOT NULL без них не встанет — снимаем вместе со схемой.
    op.execute("DELETE FROM freelancer_shift_settlement WHERE attendance_entry_id IS NULL")
    op.alter_column(
        "freelancer_shift_settlement",
        "attendance_entry_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )


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

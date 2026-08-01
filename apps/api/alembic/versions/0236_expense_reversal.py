"""Откат признанного расхода: журнал сторно и отдельное право.

Revision ID: 0236_expense_reversal
Revises: 0235_informational_doc
Create Date: 2026-08-01

Признание — не бухгалтерская запись «навсегда»: человек мог указать период шире, чем услуга
реально оказана, или контрагент вернул часть денег. Откат возвращает сумму из расхода обратно
в дебиторку, целиком или частью: признали 3 000 ₽, откатили 1 000 — эта тысяча снова «нам
должны закрыть документами или вернуть».

Отдельное право, а не ``accounting.suppliers.edit``: откат меняет прибыль уже закрытого месяца,
и это осознанно более узкое действие, чем правка расчётов с поставщиком. По той же причине рядом
живёт ``accounting.service_periods.correct_recognized``.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.auth.permissions import PERMISSION_CATALOG

revision = "0236_expense_reversal"
down_revision = "0235_informational_doc"
branch_labels = None
depends_on = None

NEW_CODES = ("accounting.expenses.reverse",)
GRANT_ROLES = ("owner", "admin")


def upgrade() -> None:
    op.create_table(
        "supplier_expense_reversal",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("accrual_id", sa.UUID(as_uuid=True), nullable=False),
        # Сколько сняли этим действием. Откатов по одному начислению может быть несколько.
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        # Сумма расхода ДО отката — чтобы историю можно было прочитать, не пересчитывая цепочку.
        sa.Column("amount_before", sa.Numeric(14, 2), nullable=False),
        sa.Column("recognition_month", sa.Date(), nullable=True),
        sa.Column("actor_user_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("amount > 0", name="ck_supplier_expense_reversal_amount"),
        sa.ForeignKeyConstraint(
            ["accrual_id"], ["supplier_expense_accrual.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_supplier_expense_reversal_accrual",
        "supplier_expense_reversal",
        ["accrual_id"],
    )

    _upsert_permissions()
    _grant_permissions(GRANT_ROLES, NEW_CODES)


def downgrade() -> None:
    codes = _sql_values(NEW_CODES)
    op.execute(
        f"delete from role_permission_event using permission "
        f"where role_permission_event.permission_id = permission.id "
        f"and permission.code in ({codes})"
    )
    op.execute(
        f"delete from role_permission using permission "
        f"where role_permission.permission_id = permission.id "
        f"and permission.code in ({codes})"
    )
    op.execute(f"delete from permission where code in ({codes})")

    op.drop_index("ix_supplier_expense_reversal_accrual", table_name="supplier_expense_reversal")
    op.drop_table("supplier_expense_reversal")


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
    op.execute(
        f"""
        insert into role_permission (role_id, permission_id)
        select role.id, permission.id
        from role cross join permission
        where role.code in ({_sql_values(role_codes)})
          and permission.code in ({_sql_values(permission_codes)})
        on conflict (role_id, permission_id) do nothing
        """
    )


def _sql_values(values: tuple[str, ...]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)

"""Леджер выплат сотрудникам ``employee_payout`` + статья ДДС «Зарплата собственника».

Общий леджер «выплачено» для двух сценариев: (1) ЗП собственника «по востребованию» — оклад в
режиме ``on_demand`` начисляется в долг с 1-го числа, гасится произвольными суммами по кнопке
«Включить в выплату»; (2) разовая «Выплата сотруднику» из плавающей кнопки. Каждая выплата —
реальный расход денег (out-``CashflowTransaction`` со статьёй ДДС, ``source_kind='employee_payout'``).

Долг собственника = Σ начислено (on_demand-строки финализированных ведомостей) − Σ ``amount``
его payout'ов. Наличный источник гасит сразу (``status='paid'``); банковский источник (T-Bank/ИП)
в Треке B заведёт черновик + транзит на Сейф с резервом (доп. поля появятся отдельной миграцией).

Статья ДДС «Зарплата собственника» (``zarplata_sobstvennika``) — оплата труда собственника
отдельной строкой ДДС (operating outflow), чтобы отделить её от «Зарплата административного
персонала».

Revision ID: 0153_employee_payout
Revises: 0152_recovery_override
Create Date: 2026-07-02
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.auth.permissions import PERMISSION_CATALOG

revision = "0153_employee_payout"
down_revision = "0152_recovery_override"
branch_labels = None
depends_on = None

# Право на создание выплат сотрудникам (ЗП собственника «по востребованию», разовые).
# Грант по умолчанию owner/admin/manager (Собственник платит себе; Управляющий ведёт ЗП).
NEW_PERMISSION_CODES = ("payroll.employee_payouts.create",)
GRANT_ROLES = ("owner", "admin", "manager")


def upgrade() -> None:
    op.create_table(
        "employee_payout",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "kind", sa.String(length=32), nullable=False, server_default="owner_salary"
        ),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("payout_date", sa.Date(), nullable=False),
        sa.Column("wallet_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("article_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "cashflow_transaction_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="paid"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount > 0", name="ck_employee_payout_amount_positive"),
        sa.CheckConstraint(
            "status in ('draft', 'pending', 'paid', 'failed', 'cancelled')",
            name="ck_employee_payout_status",
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallet.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["article_id"], ["dds_articles.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["cashflow_transaction_id"], ["cashflow_transactions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_employee_payout_employee", "employee_payout", ["employee_id"])
    op.create_index("ix_employee_payout_status", "employee_payout", ["status"])

    # Статья ДДС «Зарплата собственника» — оплата труда собственника отдельной строкой.
    op.execute(
        """
        INSERT INTO dds_articles (id, code, name, movement_type, activity_type, is_active)
        VALUES (gen_random_uuid(), 'zarplata_sobstvennika',
                'Зарплата собственника', 'outflow', 'operating', true)
        ON CONFLICT (code) DO UPDATE SET is_active = true
        """
    )

    # Право на создание выплат сотрудникам + грант ролям.
    _upsert_permissions()
    _grant_permissions(GRANT_ROLES, NEW_PERMISSION_CODES)


def downgrade() -> None:
    codes = _sql_values(NEW_PERMISSION_CODES)
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

    op.execute("DELETE FROM dds_articles WHERE code = 'zarplata_sobstvennika'")
    op.drop_index("ix_employee_payout_status", table_name="employee_payout")
    op.drop_index("ix_employee_payout_employee", table_name="employee_payout")
    op.drop_table("employee_payout")


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

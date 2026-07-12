"""Контроль ошибочных цен накладных по скользящему среднему + право подтверждения.

Позиция накладной, цена которой сильно отклонилась от скользящего среднего закупок этого товара
(порог +10% сверху / −15% снизу, окно 60 дней, минимум 3 закупки — всё переопределяется ключами
AppSetting ``invoice.price_control.*``), помечает всю накладную «подозрительной»
(``price_control_status='flagged'``). Пока накладная не подтверждена — оплата и отправка в банк
заблокированы. Новое точечное право ``invoices.confirm_price`` (owner/admin/менеджер) разрешает
нажать «ОК, всё верно» и разблокировать. Любая правка позиций сбрасывает подтверждение.

Revision ID: 0184_invoice_price_control
Revises: 0183_draft_topup_only
Create Date: 2026-07-12
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.auth.permissions import PERMISSION_CATALOG

revision = "0184_invoice_price_control"
down_revision = "0183_draft_topup_only"
branch_labels = None
depends_on = None

NEW_CODES = ("invoices.confirm_price",)
GRANT_ROLES = ("owner", "admin", "manager")


def upgrade() -> None:
    op.add_column(
        "supplier_invoice",
        sa.Column(
            "price_control_status",
            sa.String(length=16),
            nullable=False,
            server_default="clean",
        ),
    )
    op.add_column(
        "supplier_invoice",
        sa.Column(
            "price_anomalies",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "supplier_invoice",
        sa.Column("price_confirmed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "supplier_invoice",
        sa.Column("price_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_supplier_invoice_price_confirmed_by_user",
        "supplier_invoice",
        "user",
        ["price_confirmed_by_user_id"],
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
        "fk_supplier_invoice_price_confirmed_by_user", "supplier_invoice", type_="foreignkey"
    )
    op.drop_column("supplier_invoice", "price_confirmed_at")
    op.drop_column("supplier_invoice", "price_confirmed_by_user_id")
    op.drop_column("supplier_invoice", "price_anomalies")
    op.drop_column("supplier_invoice", "price_control_status")


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

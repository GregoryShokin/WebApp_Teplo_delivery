"""КДС-лайт: снимок активных заказов + журнал тапов упаковки.

``kds_order`` — снимок активного заказа доставки из iikoCloud (вебхук + сверочный
поллинг) плюс текущее состояние трёх тапов упаковки (Готово / Ждёт курьера / Передан).
``kds_order_event`` — append-only журнал тапов: источник истины, идемпотентность
офлайн-буфера планшета по ``client_event_id`` и откаты ошибочных отметок.
См. модели ``app.models.kds``.

Revision ID: 0131_kds_tables
Revises: 0130_safe_allocations
Create Date: 2026-06-21
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0131_kds_tables"
down_revision = "0130_safe_allocations"
branch_labels = None
depends_on = None

_TAP_TYPES = ("ready", "waiting_courier", "handed")
_ACTIONS = ("set", "rollback", "edit")
_SOURCES = ("kiosk", "manager")


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join("'" + value + "'" for value in values)


def upgrade() -> None:
    op.create_table(
        "kds_order",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("iiko_order_id", sa.Text(), nullable=False),
        sa.Column("order_number", sa.Text(), nullable=True),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("complete_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("when_created", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_preorder", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("has_hot_item", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("waiting_courier_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handed_to_courier_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "raw", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["last_actor_user_id"], ["user.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("iiko_order_id", name="uq_kds_order_iiko_order_id"),
    )
    op.create_index("ix_kds_order_work_date", "kds_order", ["work_date"])
    op.create_index(
        "ix_kds_order_active_work_date", "kds_order", ["is_active", "work_date"]
    )

    op.create_table(
        "kds_order_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kds_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("iiko_order_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("action", sa.String(16), nullable=False, server_default="set"),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="kiosk"),
        sa.Column("client_event_id", sa.Text(), nullable=False),
        sa.Column("previous_value", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["kds_order_id"], ["kds_order.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            f"event_type in ({_sql_in(_TAP_TYPES)})",
            name="ck_kds_order_event_event_type",
        ),
        sa.CheckConstraint(
            f"action in ({_sql_in(_ACTIONS)})",
            name="ck_kds_order_event_action",
        ),
        sa.CheckConstraint(
            f"source in ({_sql_in(_SOURCES)})",
            name="ck_kds_order_event_source",
        ),
        sa.UniqueConstraint("client_event_id", name="uq_kds_order_event_client_event_id"),
    )
    op.create_index(
        "ix_kds_order_event_order_effective",
        "kds_order_event",
        ["iiko_order_id", "effective_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_kds_order_event_order_effective", table_name="kds_order_event")
    op.drop_table("kds_order_event")
    op.drop_index("ix_kds_order_active_work_date", table_name="kds_order")
    op.drop_index("ix_kds_order_work_date", table_name="kds_order")
    op.drop_table("kds_order")

"""Снимок баланса: замороженные строки, флаги честности, ручные значения.

Четыре таблицы вместо шести из старой спеки. Не заведены: отдельная таблица коэффициентов
(семь показателей — арифметика над замороженными строками, вторая копия дала бы второй источник
правды без выгоды), таблица контрольной сверки и журнал аномалий (это флаги, у них своя таблица).

Ключевое решение, которое эта миграция закрепляет: снимок ХРАНИТ числа. Прошлое в системе
законно двигается — исключение банковской операции не датировано, стоимость основного средства
переписывается задним числом, отметка выплаты при снятии удаляется физически, разметка товара не
имеет периода действия. Пересчитываемый баланс означал бы, что октябрьская правка августа
переезжает в расхождение сентября, и контроль второго месяца перестаёт быть контролем.

Revision ID: 0274_balance_snapshot
Revises: 0273_prepayment_settled_on
Create Date: 2026-08-08
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0274_balance_snapshot"
down_revision = "0273_prepayment_settled_on"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "balance_snapshot",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("method", sa.String(16), nullable=False, server_default="inventory"),
        sa.Column("asset_total", sa.Numeric(16, 2), nullable=False),
        sa.Column("liability_total", sa.Numeric(16, 2), nullable=False),
        sa.Column("equity_plug", sa.Numeric(16, 2), nullable=False),
        sa.Column("net_profit", sa.Numeric(16, 2), nullable=True),
        sa.Column("net_profit_status", sa.String(24), nullable=True),
        sa.Column("net_profit_missing", postgresql.JSONB(), nullable=True),
        sa.Column("equity_dividends", sa.Numeric(16, 2), nullable=False, server_default="0"),
        sa.Column("equity_contributions", sa.Numeric(16, 2), nullable=False, server_default="0"),
        sa.Column("equity_returns", sa.Numeric(16, 2), nullable=False, server_default="0"),
        sa.Column(
            "prev_snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("balance_snapshot.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("recon_delta_equity", sa.Numeric(16, 2), nullable=True),
        sa.Column("recon_expected", sa.Numeric(16, 2), nullable=True),
        sa.Column("recon_residual", sa.Numeric(16, 2), nullable=True),
        sa.Column("tolerance_band", sa.Numeric(16, 2), nullable=True),
        sa.Column("residual_reason", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "finalized_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("as_of", name="uq_balance_snapshot_as_of"),
        sa.CheckConstraint("status in ('draft', 'final')", name="ck_balance_snapshot_status"),
        sa.CheckConstraint("method in ('inventory', 'rolling')", name="ck_balance_snapshot_method"),
    )
    op.create_index("ix_balance_snapshot_period", "balance_snapshot", ["period_month"])

    op.create_table(
        "balance_snapshot_line",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("balance_snapshot.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("line_code", sa.String(64), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("block", sa.String(16), nullable=False),
        sa.Column("group_code", sa.String(64), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("sign", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("kind", sa.String(16), nullable=False, server_default="source"),
        sa.Column("amount", sa.Numeric(16, 2), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="ok"),
        sa.Column("source_kind", sa.String(48), nullable=True),
        sa.Column("source_note", sa.Text(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("snapshot_id", "line_code", name="uq_balance_snapshot_line"),
        sa.CheckConstraint(
            "kind in ('source', 'memo', 'subtotal')", name="ck_balance_snapshot_line_kind"
        ),
    )
    op.create_index(
        "ix_balance_snapshot_line_snapshot", "balance_snapshot_line", ["snapshot_id", "sort_order"]
    )

    op.create_table(
        "balance_snapshot_flag",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("balance_snapshot.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("amount", sa.Numeric(16, 2), nullable=False, server_default="0"),
        sa.Column("rows", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("snapshot_id", "code", name="uq_balance_snapshot_flag"),
        sa.CheckConstraint(
            "severity in ('blocking', 'warning', 'info')",
            name="ck_balance_snapshot_flag_severity",
        ),
    )

    op.create_table(
        "balance_manual_value",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("line_code", sa.String(64), nullable=False),
        sa.Column("amount", sa.Numeric(16, 2), nullable=False),
        # Основание обязательно: цифра без него неотличима от опечатки.
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source_document", sa.Text(), nullable=True),
        sa.Column(
            "author_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("as_of", "line_code", name="uq_balance_manual_value_slot"),
    )


def downgrade() -> None:
    op.drop_table("balance_manual_value")
    op.drop_table("balance_snapshot_flag")
    op.drop_index("ix_balance_snapshot_line_snapshot", table_name="balance_snapshot_line")
    op.drop_table("balance_snapshot_line")
    op.drop_index("ix_balance_snapshot_period", table_name="balance_snapshot")
    op.drop_table("balance_snapshot")

"""Закрытый учётный месяц: его цифры менять уже нельзя.

У признанного расхода появился потребитель — отчёт «расход по месяцам». С этого момента цифра
закрытого месяца перестала быть внутренним делом витрины: её увидят в P&L и сверят с банком.
А изменить её сейчас можно молча — правкой периода, откатом, повторным признанием.

Замок, а не снимок. У основных средств закрытие месяца делает копию строк баланса
(``asset_balance_snapshot``), и там иначе нельзя: остаточная стоимость пересчитывается задним
числом при каждой коррекции. В расчётах с контрагентами пересчёта задним числом нет — начисление
меняется только осознанным действием человека. Достаточно эти действия запретить, и вторая копия
данных, которая сама стала бы источником расхождений, не понадобится.

Открыть месяц обратно можно тем же правом ``accounting.periods.close``: замок нужен не ради
неприкосновенности, а чтобы закрытый отчёт нельзя было изменить НЕЗАМЕТНО.

Revision ID: 0242_accounting_period_close
Revises: 0241_prepayment_opening_flag
Create Date: 2026-08-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0242_accounting_period_close"
down_revision = "0241_prepayment_opening_flag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounting_period_close",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column(
            "closed_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("period_month", name="uq_accounting_period_close_month"),
    )


def downgrade() -> None:
    op.drop_table("accounting_period_close")

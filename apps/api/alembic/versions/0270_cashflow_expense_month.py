"""Месяц признания расхода у проводки ДДС без контрагента.

Платёж без контрагента живёт вне ДЗ/КЗ, где хранятся периоды услуг, — поэтому у него не было
никакого способа сказать «это оплата за прошлый месяц». Наличная коммуналка июля 2026 несла
период только в комментарии («Оплата за Июнь», «Предоплата за Июль»), и отчёт валил все
платежи в месяц денег. Поле читает ОПиУ: расход встаёт в expense_month, деньги остаются
в своём месяце с явным вердиктом «учтён в другом месяце».

Revision ID: 0270_cashflow_expense_month
Revises: 0269_merge_pnl_into_main
Create Date: 2026-08-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0270_cashflow_expense_month"
down_revision = "0269_merge_pnl_into_main"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cashflow_transactions",
        sa.Column("expense_month", sa.Date(), nullable=True),
    )
    # Частичный индекс: поле заполняется у единиц проводок, а ОПиУ ищет по нему все
    # «переехавшие» расходы месяца — полный скан таблицы здесь ни к чему.
    op.create_index(
        "ix_cashflow_transactions_expense_month",
        "cashflow_transactions",
        ["expense_month"],
        postgresql_where=sa.text("expense_month is not null"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_cashflow_transactions_expense_month",
        table_name="cashflow_transactions",
        postgresql_where=sa.text("expense_month is not null"),
    )
    op.drop_column("cashflow_transactions", "expense_month")

"""Свести адресный подбор зачётов (main) с линией ОПиУ в одну голову.

ВТОРАЯ ТАКАЯ РЕВИЗИЯ ЗА НЕДЕЛЮ, И ПРИЧИНА ТА ЖЕ. Ветка ОПиУ живёт дольше одного релиза, а
основная линия за это время выкатывает свои миграции от общего предка ``0256``. Так вышло с
``0269``, так вышло и здесь: ``0257_allocation_match_basis`` (адресный подбор аванса под
закрывающий, на проде с прошлой недели) и ``0270_cashflow_expense_month`` (месяц расхода
у проводки ДДС) — две головы от одного корня, и ``alembic upgrade head`` на таком дереве
отказывается работать вовсе.

Схему ревизия не трогает: обе ветки меняли разные таблицы (``invoice_payment_allocation`` и
``cashflow_transactions``), и сводить в них нечего. Её работа — объявить порядок.

Revision ID: 0271_merge_allocation_pnl
Revises: 0270_cashflow_expense_month, 0257_allocation_match_basis
Create Date: 2026-08-06
"""

from __future__ import annotations

revision = "0271_merge_allocation_pnl"
down_revision = ("0270_cashflow_expense_month", "0257_allocation_match_basis")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Только сведение веток — DDL здесь нет и быть не должно."""


def downgrade() -> None:
    """Симметрично: откат разводит ветки обратно, ничего не отменяя."""

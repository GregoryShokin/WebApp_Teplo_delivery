"""Касса: реальная наличная выручка из OLAP (тип оплаты «Наличные»).

Поле iiko `salesCash` в кассовой смене ЗАВЫШАЕТ физический нал (включает зачтённые
предоплаты/депозиты). Достоверная наличная выручка берётся из OLAP-отчёта SALES по типам
оплат (`PayTypes.Group='CASH'`) и хранится в новой колонке ``iiko_cash_shift.cash_sales``.
Расхождение и витрина считают наличную выручку именно от неё.

Revision ID: 0120_kassa_cash_sales
Revises: 0119_kassa_permissions
Create Date: 2026-06-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0120_kassa_cash_sales"
down_revision = "0119_kassa_permissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("iiko_cash_shift", sa.Column("cash_sales", sa.Numeric(18, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("iiko_cash_shift", "cash_sales")

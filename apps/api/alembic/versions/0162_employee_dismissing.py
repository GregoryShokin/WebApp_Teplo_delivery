"""Статус сотрудника ``dismissing``: отложенное увольнение до закрытия расчётов.

По кнопке «Уволить» сотрудник переходит в ``dismissing`` (сразу деактивируется в
iiko и снимается с графика, но локально НЕ становится ``inactive``). Система
авто-переводит его в ``inactive`` только когда закрыты ВСЕ расчёты: депозит=0,
нет невыплаченной ЗП, погашены займы, применены отложенные штрафы ревизии.

Миграция добавляет только значение enum. ``ADD VALUE`` вынесен в autocommit-блок
(паттерн 0118). ``DROP VALUE`` PostgreSQL не поддерживает — downgrade значение
enum не удаляет (существующие записи в ``dismissing`` при откате кода стоит
предварительно перевести в ``inactive`` вручную).

Revision ID: 0162_employee_dismissing
Revises: 0161_expense_safe_draft
Create Date: 2026-07-07
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0162_employee_dismissing"
down_revision = "0161_expense_safe_draft"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            sa.text("ALTER TYPE employee_status ADD VALUE IF NOT EXISTS 'dismissing'")
        )


def downgrade() -> None:
    # PostgreSQL не поддерживает удаление значения enum — no-op.
    pass

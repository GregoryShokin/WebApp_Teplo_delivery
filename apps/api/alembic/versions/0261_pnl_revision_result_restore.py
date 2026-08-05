"""Вернуть строке ОПиУ фактический результат проведённых ревизий.

Revision ID: 0261_pnl_revision_result
Revises: 0260_pnl_stock_rollforward
Create Date: 2026-08-04
"""

from __future__ import annotations

from alembic import op

revision = "0261_pnl_revision_result"
down_revision = "0260_pnl_stock_rollforward"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE pnl_line
        SET title = 'Результаты ревизий',
            source_note = 'Недостача всего по проведённым ревизиям iiko минус удержанные '
                'штрафы по ревизиям. Излишки показываются в леджере справочно и недостачу '
                'не уменьшают. Складской оборот используется только для баланса.'
        WHERE code = 'audit_results'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE pnl_line
        SET title = 'Результат складского учёта',
            source_note = 'Фактический расход складских товаров (остаток на начало + '
                'приходы − остаток на конец) минус нормативный фудкост; штрафы по '
                'ревизиям уменьшают результат отдельным компонентом'
        WHERE code = 'audit_results'
        """
    )

"""Чистка шумных комментариев штрафов ревизий в расчётных листах.

Комментарии строк ``payroll_adjustment`` по ревизионным штрафам копили
информационный шум, не нужный сотруднику в персональном отчёте:

1. Недостача по ревизии — к строке «Недостача по ревизии <дата>» добавлялись
   построчные детали пересортов/номенклатуры («Пересорт <группа>: семга ХК …,
   итог …»). Оставляем только первую строку.
2. Распределённый штраф — к «Распределённый штраф ревизии <дата>, доля X/Y»
   добавлялась произвольная причина (номенклатура/имена/«перенос»). Обрезаем
   причину, оставляя «… доля X/Y».

Генераторы комментариев уже исправлены (новые строки чистые); эта миграция
причёсывает уже существующие. Идемпотентна: повторный прогон ничего не меняет
(нет переноса строки / нет «: причина» — UPDATE не затрагивает строку). Суммы и
прочие поля не трогаются. Разбивка по пересортам остаётся в админ-экране ревизии.

Revision ID: 0148_revision_comment_cleanup
Revises: 0147_payment_page_article
Create Date: 2026-06-29
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0148_revision_comment_cleanup"
down_revision = "0147_payment_page_article"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    # 1) Недостача: оставить только первую строку («Недостача по ревизии <дата>»).
    conn.execute(
        sa.text(
            """
            update payroll_adjustment
               set comment = split_part(comment, chr(10), 1)
             where comment like 'Недостача по ревизии %'
               and position(chr(10) in comment) > 0
            """
        )
    )
    # 2) Распределённый штраф: отрезать причину после «доля X/Y». Дата в префиксе —
    # ISO/UUID без двоеточий, поэтому первое ": " всегда разделитель причины.
    conn.execute(
        sa.text(
            """
            update payroll_adjustment
               set comment = left(comment, position(': ' in comment) - 1)
             where comment like 'Распределённый штраф ревизии %, доля %: %'
               and position(': ' in comment) > 0
            """
        )
    )


def downgrade() -> None:
    # Удалённый шум восстановить нельзя — даунгрейд no-op.
    pass

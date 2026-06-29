"""Категория «Внештатный» (freelancer) для ролей поваров.

Включает доступность пары роль×категория `freelancer` для всех ролей поваров
(Сушист, Пиццерист, Шаурмист, Заготовщик) в таблице
``payroll_role_category_availability``. До этого `freelancer` была помечена
deprecated и выключена для всех ролей, из-за чего назначение «повар · Внештатный»
считалось невалидным → сотрудник вис в статусе ``requires_setup`` и не попадал в
график. Коэффициент `freelancer` в проценте = 0 (как у стажёра), поэтому это
внешний персонал с ручной оплатой — на авто-расчёт ЗП не влияет.

`position_group` в этой таблице — русские лейблы ролей (см. PAYROLL_ROLE_LABELS).
Идемпотентно: строки могли отсутствовать на проде (изначальный засев матрицы в
0010 шёл из payroll_rate), поэтому INSERT ... ON CONFLICT DO UPDATE.

Кассира (Администратор) намеренно не трогаем — оба внештатных сотрудника повара.

Revision ID: 0146_freelancer_cooks
Revises: 0145_draft_status_deleted
Create Date: 2026-06-29
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0146_freelancer_cooks"
down_revision = "0145_draft_status_deleted"
branch_labels = None
depends_on = None

COOK_ROLE_GROUPS = ("Сушист", "Пиццерист", "Шаурмист", "Заготовщик")


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            insert into payroll_role_category_availability (
                position_group, category, is_enabled
            )
            select unnest(cast(:groups as text[])), 'freelancer', true
            on conflict (position_group, category)
            do update set is_enabled = true
            """
        ),
        {"groups": list(COOK_ROLE_GROUPS)},
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            update payroll_role_category_availability
               set is_enabled = false
             where category = 'freelancer'
               and position_group = any(cast(:groups as text[]))
            """
        ),
        {"groups": list(COOK_ROLE_GROUPS)},
    )

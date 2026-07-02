"""Должность «Помощник менеджера» (окладник, админ-персонал) + дефолтный оклад 6000 ₽.

Новая административная окладная должность: архетип ``okladnik`` + группа прав ``administration``
→ попадает в «Оклады администрации» (``okladnik_positions``) и в полумесячную админ-ведомость
(``admin_payroll_positions``). Дефолтный оклад по должности — ``PayrollRate`` (employee_id=NULL,
category='admin', rate_type='monthly', amount=6000). Доступ (RBAC) не выдаём —
``participates_in_access`` по умолчанию false (должность только для расчёта ЗП).

Revision ID: 0156_assistant_mgr
Revises: 0155_employee_payout_incl
Create Date: 2026-07-02
"""

from __future__ import annotations

from alembic import op

revision = "0156_assistant_mgr"
down_revision = "0155_employee_payout_incl"
branch_labels = None
depends_on = None

_POSITION_NAME = "Помощник менеджера"


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO position (id, name, archetype, permission_group, status, schedule_type)
        VALUES (gen_random_uuid(), 'Помощник менеджера', 'okladnik', 'administration',
                'active', 'FIXED')
        ON CONFLICT (name) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO payroll_rate
            (id, position_group, category, rate_type, amount, effective_from, is_active)
        SELECT gen_random_uuid(), 'Помощник менеджера', 'admin', 'monthly', 6000,
               DATE '2026-01-01', true
        WHERE NOT EXISTS (
            SELECT 1 FROM payroll_rate
            WHERE position_group = 'Помощник менеджера'
              AND category = 'admin'
              AND rate_type = 'monthly'
              AND employee_id IS NULL
        )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM payroll_rate
        WHERE position_group = 'Помощник менеджера'
          AND category = 'admin'
          AND rate_type = 'monthly'
          AND employee_id IS NULL
        """
    )
    op.execute("DELETE FROM position WHERE name = 'Помощник менеджера'")

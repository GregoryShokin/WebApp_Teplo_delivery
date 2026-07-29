"""Признак «смена ещё идёт» у явки + защита оплаченной явки от сноса.

Две правки одной миграцией — они лечат один и тот же контур посменной выдачи внештатника.

1. ``attendance_entry.is_open`` — явный признак того, что смена НЕ закрыта в iiko, а время
   её окончания синтезировал загрузчик (``build_attendance_entry``: iiko не отдал ``dateTo``
   → подставляем ``min(22:00, старт+12ч)``). До этой колонки единственным следом была строка
   ``open_shift_auto_closed`` в свободном текстовом поле ``notes``, у которой не было ни
   одного читателя: касса считала идущую смену закрытой и предлагала выдать за неё ПОЛНУЮ
   договорную ставку уже в первый час работы. Бэкфилл проставляет признак по этой самой
   метке — чтобы уже загруженные сегодняшние явки стали честными без ожидания синка.

2. ``freelancer_shift_settlement.attendance_entry_id`` переводится с ``ON DELETE CASCADE`` на
   ``ON DELETE RESTRICT``. Перезагрузка явок (``load_attendance_entries(force_reload=True)``,
   ночное окно 00:30 и кнопка «Синхронизировать смены») физически сносит явки периода —
   каскад уносил вместе с ними и сами факты наличных выдач, после чего смена снова висела
   «к выдаче», а ведомость переставала вычитать выданное. Теперь БД не даст удалить
   оплаченную явку; загрузчик такие явки обходит стороной.

Revision ID: 0218_attendance_open_shift
Revises: 0217_intake_companion
Create Date: 2026-07-29
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0218_attendance_open_shift"
down_revision = "0217_intake_companion"
branch_labels = None
depends_on = None

# Имя FK генерится конвенцией с усечением (на проде —
# fk_freelancer_shift_settlement_attendance_entry_id_atte_a4d0), поэтому ищем его по составу,
# а не по строке: так миграция одинаково ложится на прод, дев и тестовую БД.
_DROP_OLD_FK = """
DO $$
DECLARE
    fk_name text;
BEGIN
    SELECT con.conname INTO fk_name
    FROM pg_constraint con
    WHERE con.conrelid = 'freelancer_shift_settlement'::regclass
      AND con.contype = 'f'
      AND con.confrelid = 'attendance_entry'::regclass;
    IF fk_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE freelancer_shift_settlement DROP CONSTRAINT %I', fk_name);
    END IF;
END $$;
"""


def upgrade() -> None:
    op.add_column(
        "attendance_entry",
        sa.Column(
            "is_open",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Бэкфилл по исторической метке загрузчика: явки, чьё закрытие было синтезировано.
    op.execute(
        "UPDATE attendance_entry SET is_open = true WHERE notes LIKE '%open_shift_auto_closed%'"
    )

    op.execute(_DROP_OLD_FK)
    op.create_foreign_key(
        "fk_freelancer_settlement_attendance_entry",
        "freelancer_shift_settlement",
        "attendance_entry",
        ["attendance_entry_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_freelancer_settlement_attendance_entry",
        "freelancer_shift_settlement",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_freelancer_shift_settlement_attendance_entry_id_atte_a4d0",
        "freelancer_shift_settlement",
        "attendance_entry",
        ["attendance_entry_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_column("attendance_entry", "is_open")

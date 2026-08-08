"""Хозяйственная дата у леджеров сотрудника: депозит, накопительный фонд, закрытие аванса.

Балансу нужен ответ «сколько мы были должны сотрудникам 31 августа», а у трёх леджеров даты
события не было вовсе — только ``created_at``, то есть момент записи в базу. Разница не
академическая: удержание депозита за неделю 25–31.08 записывается в момент финализации
ведомости, а это уже сентябрь. По ``created_at`` августовский срез потерял бы удержание, а
сентябрьский получил бы его дважды — вместе с сентябрьским.

ПОЧЕМУ ЗНАЧЕНИЯ РАЗНЫЕ У СТРОК С ПРОГОНОМ И БЕЗ НЕГО. Строку, рождённую ведомостью, датирует
ПЕРИОД РАБОТЫ: её хозяйственный день — ``payroll_period.end_date`` (решение владельца
08.08.2026). Неделя 25–31.08 платится 2 сентября, и по дате выплаты удержание уехало бы в
сентябрь — хотя заработаны эти деньги в августе и в августовском балансе им и место. Ручную строку
(``run_id IS NULL``) датировать нечем, кроме момента ввода, — берём его, но по Москве: запись,
сделанная 1 сентября в 00:30 МСК, в UTC ещё 31 августа, и по UTC-дате она уехала бы в чужой
месяц.

``closed_on`` у аванса — дата, когда заём перестал быть долгом (списан или отменён). Бэкфилл
берёт ``updated_at``, и это ПРИБЛИЖЕНИЕ: последнее касание строки, а не дата решения. Поэтому
расчёт обязан помечать такие суммы приблизительными, а не выдавать за факт.

NOT NULL здесь не ставится сознательно: колонки заполняются кодом с этого момента, а прошлое
восстановлено бэкфиллом, качество которого разное. Жёсткое ограничение прятало бы это различие.

Revision ID: 0272_staff_ledger_happened_on
Revises: 0271_merge_allocation_pnl
Create Date: 2026-08-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0272_staff_ledger_happened_on"
down_revision = "0271_merge_allocation_pnl"
branch_labels = None
depends_on = None

# Момент записи → календарный день по Москве. Именно так считает дату остальной код
# (``services/pnl/sources/deposits.py``), и расхождение здесь дало бы сдвиг на сутки у всего,
# что заведено между полуночью и тремя часами ночи.
_MSK_DATE = "((created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow')::date"


def upgrade() -> None:
    op.add_column("deposit_transaction", sa.Column("happened_on", sa.Date(), nullable=True))
    op.add_column(
        "accumulation_fund_transaction", sa.Column("happened_on", sa.Date(), nullable=True)
    )
    op.add_column("salary_advance", sa.Column("closed_on", sa.Date(), nullable=True))

    # Строки ведомости — последним днём периода работы, а не днём выплаты.
    for table in ("deposit_transaction", "accumulation_fund_transaction"):
        op.execute(
            f"""
            UPDATE {table} AS t
               SET happened_on = p.end_date
              FROM payroll_run r
              JOIN payroll_period p ON p.id = r.period_id
             WHERE t.run_id = r.id
            """
        )
        # Ручные строки и строки прогонов, чей период уже удалён, — днём записи по Москве.
        op.execute(f"UPDATE {table} SET happened_on = {_MSK_DATE} WHERE happened_on IS NULL")

    op.execute(
        """
        UPDATE salary_advance
           SET closed_on = ((updated_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow')::date
         WHERE status IN ('written_off', 'cancelled')
        """
    )

    op.create_index(
        "ix_deposit_transaction_employee_happened",
        "deposit_transaction",
        ["employee_id", "happened_on"],
    )
    op.create_index(
        "ix_accumulation_fund_transaction_employee_happened",
        "accumulation_fund_transaction",
        ["employee_id", "happened_on"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_accumulation_fund_transaction_employee_happened",
        table_name="accumulation_fund_transaction",
    )
    op.drop_index("ix_deposit_transaction_employee_happened", table_name="deposit_transaction")
    op.drop_column("salary_advance", "closed_on")
    op.drop_column("accumulation_fund_transaction", "happened_on")
    op.drop_column("deposit_transaction", "happened_on")

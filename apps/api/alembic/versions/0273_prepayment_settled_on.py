"""День, когда предоплата закрыта решением человека, а не гашением документа.

Обычная предоплата гасится аллокацией — у той есть и сумма, и дата, и расчёт остатка на дату
считает именно по ним. Но есть второй путь: дозачётные остатки и ручные коррекции исторических
расчётов ставят ``status='settled'`` прямым присвоением, не создавая строки гашения вовсе
(``scripts/writeoff_pre_accounting``, разбор старых взаиморасчётов). Для расчёта на дату такая
предоплата остаётся открытой дебиторкой НАВСЕГДА: гашений у неё нет, значит вычитать нечего.
На проде это 38 479 ₽ одной строкой — «Поставка овощей», коррекция от 22.07.2026.

Даты у такого закрытия не хранилось нигде: ``updated_at`` у модели нет, есть только
``created_at``. Поэтому колонка, а не эвристика поверх существующих полей.

Бэкфилл ставит день создания — приближение, и оно занижает срок жизни дебиторки: предоплата,
созданная в июле и закрытая вручную в сентябре, по нему выглядит закрытой с июля. Для
единственной прод-строки разница в два дня и на августовский срез не влияет; для будущих строк
скрипт досписания пишет дату сам.

Revision ID: 0273_prepayment_settled_on
Revises: 0272_staff_ledger_happened_on
Create Date: 2026-08-08
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0273_prepayment_settled_on"
down_revision = "0272_staff_ledger_happened_on"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("supplier_prepayment", sa.Column("settled_on", sa.Date(), nullable=True))
    # Только строки, закрытые БЕЗ единого гашения: у остальных дата берётся из аллокаций.
    op.execute(
        """
        UPDATE supplier_prepayment AS p
           SET settled_on = ((p.created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Moscow')::date
         WHERE p.status = 'settled'
           AND NOT EXISTS (
               SELECT 1 FROM invoice_payment_allocation a WHERE a.prepayment_id = p.id
           )
        """
    )


def downgrade() -> None:
    op.drop_column("supplier_prepayment", "settled_on")

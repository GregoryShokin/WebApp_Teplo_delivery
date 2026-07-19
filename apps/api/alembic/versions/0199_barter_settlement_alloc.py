"""Значение ``barter`` в enum ``invoice_allocation_source``.

Часть 1 из 2 (вторая — ``0200_barter_alloc_link``). Отдельная ревизия ради ясности: значение
добавляется здесь, а колонка-источник и индекс — следующей.

ВАЖНО про использование: PostgreSQL запрещает применять только что добавленное значение enum в
ТОЙ ЖЕ транзакции (``asyncpg.exceptions.UnsafeNewEnumValueUsageError``), а ``env.py`` гоняет всю
цепочку миграций одной транзакцией — разделения на ревизии для этого НЕ достаточно. Каст вида
``'barter'::text::invoice_allocation_source`` не спасает, ручной ``COMMIT`` внутри миграции ломает
подъём схемы. Поэтому ни одна миграция значение не использует: перенос уже проведённых зачётов
вынесен в ``apps/api/scripts/backfill_barter_allocations.sql`` (запускается один раз после
``alembic upgrade head``).

Само значение: взаимозачёт бартерных поставок — долг закрыт ТОВАРОМ, денег не было. Ни банковской
операции, ни ДДС-проводки, ни предоплаты у такой аллокации нет (CHECK
``ck_invoice_allocation_single_source`` это допускает: 0 <= 1), источник указывает
``barter_settlement_id`` из ревизии 0200.

Revision ID: 0199_barter_settlement_alloc
Revises: 0198_barter_money_return
Create Date: 2026-07-19
"""

from __future__ import annotations

from alembic import op

revision = "0199_barter_settlement_alloc"
down_revision = "0198_barter_money_return"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE invoice_allocation_source ADD VALUE IF NOT EXISTS 'barter'")


def downgrade() -> None:
    # ALTER TYPE ... DROP VALUE в PostgreSQL не существует: значение остаётся в типе.
    # Строки, которые его несут, удаляет downgrade ревизии 0200.
    pass

"""Бартерный взаимозачёт становится аллокацией: ссылка на зачёт + перенос уже проведённых.

Часть 2 из 2 (первая — 0199_barter_settlement_alloc, добавляет значение enum отдельной
транзакцией). Имя ревизии короткое не случайно: alembic_version.version_num — varchar(32). Решение
владельца 2026-07-19.

Раньше ``_create_settlement`` закрывал накладные ПРЯМЫМ присвоением ``payment_status='paid'``,
не оставляя строки в ``invoice_payment_allocation``. Зачёт был невидим для всей машинерии, которая
считает долг по аллокациям: ``_recompute_status`` роняла статус обратно в ``unpaid``, оставляя
зачёт сиротой; FIFO правил 1/2 гасило деньгами долг, уже закрытый товаром (двойной расход);
откаты банковских операций и гейты удаления накладной его тоже не видели.

``ON DELETE CASCADE`` делает распуск зачёта атомарным: удалили ``barter_settlement`` — аллокации
ушли, статус пересчитался обычным путём.

ПЕРЕНОС УЖЕ ПРОВЕДЁННЫХ ЗАЧЁТОВ ЗДЕСЬ НЕ ДЕЛАЕТСЯ. PostgreSQL запрещает использовать значение
enum, добавленное в той же транзакции, а env.py гоняет всю цепочку одной транзакцией — любой
INSERT с 'barter' роняет миграцию (UnsafeNewEnumValueUsageError). Перенос выполняется отдельным
шагом ПОСЛЕ применения миграций: см. scripts/backfill_barter_allocations.sql.

Revision ID: 0200_barter_alloc_link
Revises: 0199_barter_settlement_alloc
Create Date: 2026-07-19
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0200_barter_alloc_link"
down_revision = "0199_barter_settlement_alloc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoice_payment_allocation",
        sa.Column("barter_settlement_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_invoice_allocation_barter_settlement",
        "invoice_payment_allocation",
        "barter_settlement",
        ["barter_settlement_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_invoice_allocation_barter_settlement",
        "invoice_payment_allocation",
        ["barter_settlement_id"],
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM invoice_payment_allocation "
        "WHERE source_kind::text = 'barter' OR barter_settlement_id IS NOT NULL"
    )
    op.drop_index("ix_invoice_allocation_barter_settlement", "invoice_payment_allocation")
    op.drop_constraint(
        "fk_invoice_allocation_barter_settlement",
        "invoice_payment_allocation",
        type_="foreignkey",
    )
    op.drop_column("invoice_payment_allocation", "barter_settlement_id")

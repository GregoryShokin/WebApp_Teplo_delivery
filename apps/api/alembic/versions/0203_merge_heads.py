"""Слияние двух линий миграций: депозиты курьеров (main) и контур ДЗ/КЗ + бартер + ОС (ветка).

Обе линии ответвились от ``0189_payroll_draft_deleted`` и развивались параллельно, поэтому после
слияния веток у alembic оказалось ДВЕ головы и ``upgrade head`` падал с «Multiple head revisions
are present»:

* main:  0191_deposit_bank_draft → 0192_deposit_draft_courier_ref
* ветка: 0190_supplier_service_periods → 0191_confirm_no_dds_article → 0192_sbis_document →
         0193…0200 (канон ДЗ/КЗ, бартер) → 0201_fixed_assets → 0202_barter_writeoff

Номера 0191/0192 заняты в обеих линиях РАЗНЫМИ файлами — для alembic это не коллизия
(``revision`` уникальны, порядок он строит по ним, а не по имени файла).

Данные линии не пересекают: main трогает ``deposit_bank_draft``, ветка — колонки поставщиков,
статью ДДС и таблицу ``sbis_document``. Поэтому слияние пустое: ни схему, ни данные не меняем.

Revision ID: 0203_merge_heads
Revises: 0192_deposit_draft_courier_ref, 0202_barter_writeoff
Create Date: 2026-07-20
"""

from __future__ import annotations

revision = "0203_merge_heads"
down_revision = ("0192_deposit_draft_courier_ref", "0202_barter_writeoff")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Слияние линий — схему не трогаем."""


def downgrade() -> None:
    """Разделение линий обратно — схему не трогаем."""

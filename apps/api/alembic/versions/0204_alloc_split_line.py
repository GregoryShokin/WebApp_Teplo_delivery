"""Банковская аллокация может нести и операцию, и проводку доли разбора.

Разбор банк-операции стал построчным по контрагентам: один платёж («овощи + коробки +
мусорщики») разносится на доли, у каждой свой контрагент и своя накладная. Чтобы «бюджет
платежа» (``payment_allocated_amount``) считался ПО ДОЛЕ, а не по всей операции, аллокация
гашения помечается обоими ключами: ``bank_operation_id`` (её видят прежние двери — «операция
уже использована в сверке», откат операции) и ``cashflow_transaction_id`` (доля-владелец).

Прежний CHECK разрешал ровно один ключ из трёх, поэтому пара «операция + проводка» падала. Новый
оставляет взаимоисключающими ДЕНЕЖНЫЙ ключ (операция и/или проводка) и ПРЕДОПЛАТНЫЙ
(``prepayment_id``): гашение против выданной предоплаты деньгами этого платежа не финансируется.

Данные не мигрируем: у существующих аллокаций ``cashflow_transaction_id`` пуст, и мост
«операция → её первая проводка» продолжает работать для них по-старому.

Revision ID: 0204_alloc_split_line
Revises: 0203_merge_heads
Create Date: 2026-07-20
"""

from __future__ import annotations

from alembic import op

revision = "0204_alloc_split_line"
down_revision = "0203_merge_heads"
branch_labels = None
depends_on = None

CONSTRAINT = "ck_invoice_allocation_single_source"
TABLE = "invoice_payment_allocation"


# Имя CHECK в БД непредсказуемо: ``op.create_check_constraint`` прогоняет его через
# naming_convention метаданных, поэтому исходное ``ck_invoice_allocation_single_source``
# превращается в обрезанное ``ck_invoice_payment_allocation_ck_invoice_allocation_sin_<hash>``.
# Дропаем по СОДЕРЖИМОМУ (единственный CHECK этой таблицы, где участвует prepayment_id) — иначе
# шаг падает на любой базе, где предыдущая миграция уже переименовала констрейнт по конвенции.
DROP_CURRENT_CHECK = f"""
DO $$
DECLARE target text;
BEGIN
    SELECT conname INTO target
      FROM pg_constraint
     WHERE conrelid = '{TABLE}'::regclass
       AND contype = 'c'
       AND pg_get_constraintdef(oid) LIKE '%prepayment_id%';
    IF target IS NOT NULL THEN
        EXECUTE format('ALTER TABLE {TABLE} DROP CONSTRAINT %I', target);
    END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(DROP_CURRENT_CHECK)
    op.create_check_constraint(
        CONSTRAINT,
        TABLE,
        "((bank_operation_id is not null or cashflow_transaction_id is not null)::int "
        "+ (prepayment_id is not null)::int) <= 1",
    )


def downgrade() -> None:
    # Пара ключей, легальная только в новой схеме: оставляем операцию (её знают все прежние
    # двери), метку доли снимаем — иначе старый CHECK не создастся.
    op.execute(
        f"UPDATE {TABLE} SET cashflow_transaction_id = NULL "
        "WHERE bank_operation_id IS NOT NULL AND cashflow_transaction_id IS NOT NULL"
    )
    op.execute(DROP_CURRENT_CHECK)
    op.create_check_constraint(
        CONSTRAINT,
        TABLE,
        "((bank_operation_id is not null)::int + (cashflow_transaction_id is not null)::int "
        "+ (prepayment_id is not null)::int) <= 1",
    )

"""Статус ``deleted`` у банк-черновика выдачи аванса/займа.

Черновик, стёртый владельцем в интернет-банке, банк отдаёт как ``PAYMENT_NOT_FOUND`` —
``classify_payment_status`` сводит это к ``deleted``. У зарплатных черновиков и черновиков
оплаты счетов такой статус уже есть, у депозитных — тоже (CHECK из 0191), а у
``salary_advance_bank_draft`` CHECK его не пускал: попытка сохранить оставила бы строку
в ``created``, то есть навсегда в корзине «Отправлен в банк».

Только расширение списка допустимых значений — данные не трогаются.

Revision ID: 0274_advance_draft_deleted
Revises: 0273_prepayment_settled_on
Create Date: 2026-08-19
"""

from __future__ import annotations

from alembic import op

revision = "0274_advance_draft_deleted"
down_revision = "0273_prepayment_settled_on"
branch_labels = None
depends_on = None

_OLD = "status in ('created', 'updated', 'paid', 'disbursed', 'failed', 'cancelled')"
_NEW = (
    "status in ('created', 'updated', 'paid', 'disbursed', 'failed', 'deleted', 'cancelled')"
)

# Имя CHECK'а в БД собрано naming-convention'ом и усечено с хэшем
# (``ck_salary_advance_bank_draft_ck_salary_advance_bank_dra_374f``), поэтому дропаем не по
# имени, а по таблице: единственный статусный CHECK узнаётся по слову 'disbursed'.
_DROP_STATUS_CHECK = """
DO $$
DECLARE conname text;
BEGIN
    SELECT c.conname INTO conname
    FROM pg_constraint c
    WHERE c.conrelid = 'salary_advance_bank_draft'::regclass
      AND c.contype = 'c'
      AND pg_get_constraintdef(c.oid) LIKE '%disbursed%';
    IF conname IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE salary_advance_bank_draft DROP CONSTRAINT %I', conname
        );
    END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(_DROP_STATUS_CHECK)
    op.create_check_constraint(
        "ck_salary_advance_bank_draft_status", "salary_advance_bank_draft", _NEW
    )


def downgrade() -> None:
    # Строгий CHECK не примет уже сохранённые deleted-черновики: возвращаем их в failed —
    # смысл тот же (платёж не прошёл, деньги не двигались), а причина остаётся в last_error.
    op.execute(
        "UPDATE salary_advance_bank_draft SET status = 'failed' WHERE status = 'deleted'"
    )
    op.execute(_DROP_STATUS_CHECK)
    op.create_check_constraint(
        "ck_salary_advance_bank_draft_status", "salary_advance_bank_draft", _OLD
    )

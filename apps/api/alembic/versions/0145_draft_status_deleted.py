"""counterparty_payment_draft: статус 'deleted' (черновик удалён в банке)

Расширяет CHECK ``ck_counterparty_payment_draft_status`` значением ``deleted``: T-Банк по
``POST /payment/status`` отдаёт ``DELETED`` для отозванного/неподписанного платёжного
черновика. ``apply_payment_status`` при этом возвращает накладные в «неоплачено» (снимает
``draft_id``) и помечает черновик ``deleted``.

CHECK дропаем ДИНАМИЧЕСКИ (по таблице, любой status-констрейнт), т.к. на проде имя искажено
naming_convention (``ck_counterparty_payment_draft_ck_counterparty_payment_d_d94a``), а в
свежесобранной БД могло быть иным — так миграция надёжна в любой среде. Создаём с явным
коротким именем (raw SQL не применяет convention).

Revision ID: 0145_draft_status_deleted
Revises: 0144_pp_exclude_schedule
Create Date: 2026-06-29
"""

from __future__ import annotations

from alembic import op

revision = "0145_draft_status_deleted"
down_revision = "0144_pp_exclude_schedule"
branch_labels = None
depends_on = None

_TABLE = "counterparty_payment_draft"
_NAME = "ck_counterparty_payment_draft_status"

_DROP_STATUS_CHECKS = f"""
DO $$
DECLARE c record;
BEGIN
  FOR c IN
    SELECT conname FROM pg_constraint
    WHERE conrelid = '{_TABLE}'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) ILIKE '%status%'
  LOOP
    EXECUTE format('ALTER TABLE {_TABLE} DROP CONSTRAINT %I', c.conname);
  END LOOP;
END $$;
"""


def upgrade() -> None:
    op.execute(_DROP_STATUS_CHECKS)
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_NAME} "
        "CHECK (status IN ('created', 'updated', 'paid', 'failed', 'deleted'))"
    )


def downgrade() -> None:
    # Вернуть удалённые в 'failed', иначе сужение CHECK упадёт на существующих строках.
    op.execute(f"UPDATE {_TABLE} SET status = 'failed' WHERE status = 'deleted'")
    op.execute(_DROP_STATUS_CHECKS)
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_NAME} "
        "CHECK (status IN ('created', 'updated', 'paid', 'failed'))"
    )

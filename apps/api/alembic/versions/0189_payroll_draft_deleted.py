"""Allow a deleted status for payroll bank drafts.

T-Bank returns ``DELETED`` when an unsigned draft is removed. Polling and the payment-status
webhook persist that outcome so an unpaid finalized payroll run can become ready to send again.

Revision ID: 0189_payroll_draft_deleted
Revises: 0188_safe_own_funds_purpose
Create Date: 2026-07-14
"""

from __future__ import annotations

from alembic import op

revision = "0189_payroll_draft_deleted"
down_revision = "0188_safe_own_funds_purpose"
branch_labels = None
depends_on = None

_TABLE = "payroll_bank_draft"
_NAME = "ck_payroll_bank_draft_status"

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
    op.execute(f"UPDATE {_TABLE} SET status = 'failed' WHERE status = 'deleted'")
    op.execute(_DROP_STATUS_CHECKS)
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_NAME} "
        "CHECK (status IN ('created', 'updated', 'paid', 'failed'))"
    )

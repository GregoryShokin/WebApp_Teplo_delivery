"""Use the owner-approved purpose for payroll transfers to Safe.

Revision ID: 0188_safe_own_funds_purpose
Revises: 0187_restore_ip_card_requisites
Create Date: 2026-07-14

This wording was explicitly requested by the owner. Do not replace it with
payroll/salary wording without another explicit owner request.
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision = "0188_safe_own_funds_purpose"
down_revision = "0187_restore_ip_card_requisites"
branch_labels = None
depends_on = None

SETTING_KEY = "payroll.bank_payout_requisites"
OWNER_APPROVED_PAYMENT_PURPOSE = (
    "Перевод собственных средств на Сейф. Период выплаты: {start}–{end}. НДС не облагается"
)


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            update app_setting
               set value = jsonb_set(
                       value,
                       '{paymentPurpose}',
                       cast(:purpose as jsonb),
                       true
                   ),
                   updated_at = now()
             where key = :key
            """
        ).bindparams(
            key=SETTING_KEY,
            purpose=json.dumps(OWNER_APPROVED_PAYMENT_PURPOSE, ensure_ascii=False),
        )
    )


def downgrade() -> None:
    # The previous wording described the transfer as salary. A downgrade must
    # not silently restore wording that the owner explicitly rejected.
    pass

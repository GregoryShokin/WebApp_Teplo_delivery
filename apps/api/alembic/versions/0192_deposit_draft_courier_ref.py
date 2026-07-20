"""Курьеры в полный цикл выдачи депозита: ссылка на получателя через ``employee_id``.

Банк-возврат депозита курьеру теперь идёт полным циклом (черновик → оплата → резерв →
выдача), как у производственников. Курьерская транзакция (``CourierDepositTransaction``,
целочисленный id) создаётся при ФАКТИЧЕСКОЙ выдаче резерва, а не при отправке черновика —
поэтому черновик ссылается на курьера через ``employee_id`` (курьер — это ``Employee``), а
``courier_deposit_transaction_id`` заполняется при выдаче.

Ослабляем check-констрейнт получателя: для ``recipient_kind='courier'`` теперь требуется
``employee_id IS NOT NULL`` (как у производственника), а ``courier_deposit_transaction_id``
становится необязательным (NULL до выдачи). Производственная ветка не меняется.

Revision ID: 0192_deposit_draft_courier_ref
Revises: 0191_deposit_bank_draft
Create Date: 2026-07-16
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0192_deposit_draft_courier_ref"
down_revision = "0191_deposit_bank_draft"
branch_labels = None
depends_on = None

_OLD = (
    "(recipient_kind = 'production' AND employee_id IS NOT NULL "
    "AND courier_deposit_transaction_id IS NULL) "
    "OR (recipient_kind = 'courier' AND courier_deposit_transaction_id IS NOT NULL)"
)
_NEW = (
    "(recipient_kind = 'production' AND employee_id IS NOT NULL "
    "AND courier_deposit_transaction_id IS NULL) "
    "OR (recipient_kind = 'courier' AND employee_id IS NOT NULL)"
)


def upgrade() -> None:
    op.drop_constraint(
        "ck_deposit_bank_draft_recipient_ref", "deposit_bank_draft", type_="check"
    )
    op.create_check_constraint(
        "ck_deposit_bank_draft_recipient_ref", "deposit_bank_draft", sa.text(_NEW)
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_deposit_bank_draft_recipient_ref", "deposit_bank_draft", type_="check"
    )
    op.create_check_constraint(
        "ck_deposit_bank_draft_recipient_ref", "deposit_bank_draft", sa.text(_OLD)
    )

"""Курьерский депозит: канал выдачи (payout_method) + наличный счёт (этап 1).

Возврат депозита курьера теперь может выдаваться с разных счетов (ТК Черникова по
умолчанию / Сейф), позже — банк-черновиком. Добавляем на courier_deposit_transaction:
- payout_method (Text nullable, NULL=legacy cash_tk),
- payout_wallet_id (FK wallet.id, ON DELETE SET NULL).

down_revision = 0130 (main-голова); kds-миграции 0131/0132 идут отдельной веткой
(другой агент, не в main) — merge-ревижн при их вливании.

Revision ID: 0133_courier_deposit_payout_method
Revises: 0130_safe_allocations
Create Date: 2026-06-21
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0133_courier_deposit_payout"
down_revision = "0130_safe_allocations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "courier_deposit_transaction",
        sa.Column("payout_method", sa.Text(), nullable=True),
    )
    op.add_column(
        "courier_deposit_transaction",
        sa.Column("payout_wallet_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_courier_deposit_transaction_payout_wallet_id_wallet",
        "courier_deposit_transaction",
        "wallet",
        ["payout_wallet_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_courier_deposit_transaction_payout_wallet_id_wallet",
        "courier_deposit_transaction",
        type_="foreignkey",
    )
    op.drop_column("courier_deposit_transaction", "payout_wallet_id")
    op.drop_column("courier_deposit_transaction", "payout_method")

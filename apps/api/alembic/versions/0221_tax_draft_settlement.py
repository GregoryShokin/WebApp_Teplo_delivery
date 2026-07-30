"""Налоговый черновик: чем закрыт и когда ушёл в банк.

У черновика ``tax_bank_draft`` не было пути выхода из ``in_bank``, кроме удачного матча со
списанием. Платёжка, которую владелец не подтвердил в банк-клиенте (или удалил там), висела
активной вечно и оставалась кандидатом разбора: жёсткими условиями матча были только
получатель и точная сумма, поэтому со временем такой черновик мог перехватить постороннее
списание в ФНС/СФР той же суммы и пометиться оплаченным.

Две колонки закрывают дыру:

``sent_to_bank_at`` — момент отправки в банк, точка отсчёта протухания. Считать от
``created_at`` нельзя: платёж готовят заранее, а отправляют к сроку уплаты, и черновик
«состарился» бы, ещё не побывав в банке. Протухший черновик перестаёт ловить списания по
сумме, но по-прежнему находится по СВОЕМУ номеру документа — реальная оплата не теряется.

``settled_operation_id`` — какая операция выписки закрыла черновик. Раньше переход в ``paid``
не оставлял следа, и разобрать задним числом «чем закрылось» было нечем. Ссылка ещё и делает
матч одноразовым: закрытый черновик из разбора выбывает.

Обе nullable и заполняются вперёд: у существующих строк (на проде — три, все ``paid``)
остаются NULL, отсчёт протухания для них падает на ``created_at``.

Revision ID: 0221_tax_draft_settlement
Revises: 0220_iiko_cash_payout
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0221_tax_draft_settlement"
down_revision = "0220_iiko_cash_payout"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tax_bank_draft",
        sa.Column("sent_to_bank_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tax_bank_draft",
        sa.Column("settled_operation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_tax_bank_draft_settled_operation",
        "tax_bank_draft",
        "bank_operations",
        ["settled_operation_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_tax_bank_draft_settled_operation", "tax_bank_draft", type_="foreignkey"
    )
    op.drop_column("tax_bank_draft", "settled_operation_id")
    op.drop_column("tax_bank_draft", "sent_to_bank_at")

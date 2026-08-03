"""Аллокация помнит СВОЕГО автора: правило 1 против ручной оплаты.

``invoice_payment_allocation.origin`` — NULL (человек / адресная дверь) либо ``'rule1'``
(авто-раскладка свободного платежа поставщику, ``ensure_prepayment_from_bank_transaction``).

Зачем. Правило 1 канона ДЗ/КЗ пересобирает своё распределение при каждой переклассификации
проводки: снимает прежние зачёты и раскладывает деньги заново. Отличать «своё» от «чужого» до
сих пор приходилось по ``doc_kind``: зачёты ЗАКРЫВАЮЩИХ считались правиловыми, оплаты СЧЕТОВ —
всегда ручными (см. ``_unwind_transaction_kz_settlements``). Пока правило 1 счета не трогало,
эвристика была верна. Теперь оно гасит и счёт (иначе оплаченный свободным платежом месяц
оставался висеть в очереди оплат как «Готов к оплате» — риск заплатить дважды), и одного
``doc_kind`` не хватает: без признака пересборка либо оставила бы свой зачёт счёта висеть
(двойной зачёт), либо снесла бы ручную оплату оператора.

БЭКФИЛЛА НЕТ, и это осознанно: все существующие оплаты счетов сделаны людьми и дверями с
адресным решением — ровно то, что означает NULL. Проставить им ``'rule1'`` значило бы отдать
автоматике право снимать чужие решения.

Revision ID: 0250_allocation_origin
Revises: 0249_owner_loan_articles
Create Date: 2026-08-03
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0250_allocation_origin"
down_revision = "0249_owner_loan_articles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoice_payment_allocation",
        sa.Column("origin", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("invoice_payment_allocation", "origin")

"""Второй документ из одного PDF-вложения («пакет счёт + УПД»).

Поставщик может слать пакет одним файлом (СДЭК: стр. 1 — счёт на оплату, стр. 2 — УПД,
стр. 3 — приложение к УПД). Раньше из вложения рождался ровно ОДИН документ, и им оказывался
УПД: маркер «универсальный передаточный документ» проверяется раньше маркеров счёта и ищется
по всему склеенному тексту. Счёт при этом терялся (в очередь оплат не попадал), а кредиторка
вставала на сумму из приложения.

Теперь ingest заводит оба документа: ``invoice_id`` — счёт (очередь оплат), новый
``companion_invoice_id`` — закрывающий. Ссылка нужна, чтобы исключение/возврат/удаление
intake вели ОБА документа: без неё УПД оставался бы сиротой в кредиторке.

Revision ID: 0217_intake_companion
Revises: 0216_merge_taxes_into_main
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0217_intake_companion"
down_revision = "0216_merge_taxes_into_main"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "email_invoice_intake",
        sa.Column("companion_invoice_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_email_intake_companion_invoice",
        "email_invoice_intake",
        "supplier_invoice",
        ["companion_invoice_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_email_intake_companion_invoice", "email_invoice_intake", type_="foreignkey"
    )
    op.drop_column("email_invoice_intake", "companion_invoice_id")

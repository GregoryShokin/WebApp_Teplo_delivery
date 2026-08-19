"""НДС платежа на банковском черновике: ставка и выделенная сумма налога.

Назначение платежа обязано называть налог («в т.ч. НДС 22% — 1 439,90») или прямо говорить
«Без НДС»: это читают банк и налоговая. У платёжки по СЧЁТУ налог давно берётся с накладной
(``vat_total``/``vat_breakdown``, см. 0097), но у платежей из окна «Новый платёж» счёта нет —
свободный расход и предоплата поставщику уходили в банк вообще без упоминания налога.

Ставку задаёт человек в окне, сумма выделяется из ИТОГА платежа («в том числе»). Обе колонки
NULL — платёж без НДС; хранятся ради разбора задним числом: назначение уже ушло в банк, и
по нему потом спрашивают, откуда взялась цифра.

Только добавление nullable-колонок — существующие черновики не трогаются.

Revision ID: 0275_payment_draft_vat
Revises: 0274_advance_draft_deleted
Create Date: 2026-08-19
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0275_payment_draft_vat"
down_revision = "0274_advance_draft_deleted"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "counterparty_payment_draft",
        sa.Column("vat_rate", sa.String(length=8), nullable=True),
    )
    op.add_column(
        "counterparty_payment_draft",
        sa.Column("vat_amount", sa.Numeric(14, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("counterparty_payment_draft", "vat_amount")
    op.drop_column("counterparty_payment_draft", "vat_rate")

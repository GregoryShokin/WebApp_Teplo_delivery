"""Входящий остаток отличается от производной дебиторки — признаком, а не текстом заметки.

Предоплата без ДДС-проводки бывает двух совершенно разных природ:

* ВХОДЯЩИЙ ОСТАТОК — деньги ушли до внедрения системы, проводки под них нет и не будет.
  Это самостоятельный денежный факт, и в сверке он обязан быть строкой.
* ПРОИЗВОДНАЯ дебиторка — выведена из уже учтённого факта: излишек оплаты накладной, оплата
  счёта, возврат суммы в дебиторку при откате расхода. Деньги там посчитаны другой строкой,
  и вторая строка задваивает остаток.

Сверка отсекала по ``kind='prepaid_bill'`` — то есть знала ровно один вид производных. Излишек
оплаты накладной ООО «АЛЬЯНС ЮГ» на 23 730 ₽ (``kind='goods'``) уводил бегущий остаток от
плитки «Остатки» ровно на эту сумму.

Отличать по ``note`` нельзя: текст заметки задаёт человек при вводе остатка.

БЭКФИЛЛ. Помечаем ``opening=true`` всё, что заведено без проводки и не является известной
производной. На проде 02.08.2026 это одна строка — «Остаток кабинета Яндекс Директ на
01.06.2026» (13 429,35 ₽), а единственная производная — тот самый излишек по накладной
DX001323A. Оплаченные счета (``prepaid_bill``) не трогаем: они производные по определению.

Revision ID: 0241_prepayment_opening_flag
Revises: 0240_cancel_bill_accruals
Create Date: 2026-08-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0241_prepayment_opening_flag"
down_revision = "0240_cancel_bill_accruals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "supplier_prepayment",
        sa.Column("opening", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.execute(
        """
        UPDATE supplier_prepayment
        SET opening = true
        WHERE cashflow_transaction_id IS NULL
          AND kind <> 'prepaid_bill'
          AND bill_invoice_id IS NULL
          AND coalesce(note, '') NOT LIKE 'Излишек оплаты по накладной%'
        """
    )


def downgrade() -> None:
    op.drop_column("supplier_prepayment", "opening")

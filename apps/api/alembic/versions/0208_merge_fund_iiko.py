"""Слияние двух голов: закрытие фонда уволенных (ветка) и сверка платежей iiko (main).

Обе линии ответвились от ``0206_invoice_operational_scope`` и получили номер 0207:

* main:  0207_iiko_payment_verify — подтверждение зеркальных платежей по проводкам iiko
* ветка: 0207_fund_forfeit_settlement — колонка ``settled_at`` у счетов накопительного фонда

Разные ``revision``-идентификаторы при одинаковом номере файла для alembic не коллизия, но две
головы валят ``upgrade head`` с «Multiple head revisions are present». Линии не пересекаются:
main трогает платежи и накладные, ветка — ``accumulation_fund_account``. Поэтому слияние пустое.

Revision ID: 0208_merge_fund_iiko
Revises: 0207_fund_forfeit_settlement, 0207_iiko_payment_verify
Create Date: 2026-07-23
"""

from __future__ import annotations

revision = "0208_merge_fund_iiko"
down_revision = ("0207_fund_forfeit_settlement", "0207_iiko_payment_verify")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Слияние линий — схему не трогаем."""


def downgrade() -> None:
    """Разделение линий обратно — схему не трогаем."""

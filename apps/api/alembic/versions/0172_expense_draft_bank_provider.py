"""Банк-плательщик свободного вывода: колонка ``bank_provider`` на черновике «Новый платёж».

Добавляет ``bank_provider`` в ``counterparty_payment_draft`` (по умолчанию ``tbank``). Значение
``sber`` задаётся ТОЛЬКО у свободного вывода на Сейф (окно «Новый платёж», expense) — накладные,
предоплата поставщику и informal-закуп остаются в Т-Банке. По колонке paid-переход книжит транзит
р/с→Сейф на счёт нужного банка (см. ``bank_payment_status._resolve_payer_bank_wallet``).

Зеркало 0168 (``bank_provider`` на payroll_bank_draft / salary_advance_bank_draft).

Revision ID: 0172_expense_draft_bank_provider
Revises: 0171_courier_deposit_delete
Create Date: 2026-07-08
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0172_expense_draft_bank_provider"
down_revision = "0171_courier_deposit_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "counterparty_payment_draft",
        sa.Column("bank_provider", sa.String(16), nullable=False, server_default="tbank"),
    )


def downgrade() -> None:
    op.drop_column("counterparty_payment_draft", "bank_provider")

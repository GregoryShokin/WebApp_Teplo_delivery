"""Строка транша «Нового платежа» → контрагент (атрибуция расхода свободного вывода).

Статья свободного вывода на Сейф контрагента не требует, но к ней могут быть привязаны
контрагенты (``counterparty_payable_profile.default_dds_article_id``, галка «Закрепить за
контрагентом»). Тогда в окне можно указать, КОМУ платим — целёвка Сейфа помечается этим
контрагентом (деньги по-прежнему идут ИП→Сейф, реквизиты контрагента не нужны).

Revision ID: 0170_expense_line_counterparty
Revises: 0169_expense_draft_lines
Create Date: 2026-07-08
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0170_expense_line_counterparty"
down_revision = "0169_expense_draft_lines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "expense_draft_line",
        sa.Column("counterparty_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_expense_draft_line_counterparty",
        "expense_draft_line",
        "counterparty",
        ["counterparty_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_expense_draft_line_counterparty", "expense_draft_line", type_="foreignkey"
    )
    op.drop_column("expense_draft_line", "counterparty_id")

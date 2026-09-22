"""Дедуп налогового вложения в пределах письма, а не всех писем по байтам.

Форма ПД для травматизма может быть побайтно одинаковой в разные месяцы. При
глобальном UNIQUE по SHA-256 письмо за сентябрь отвергалось как повтор августа
до того, как парсер мог определить месяц по дате письма.

Revision ID: 0277_tax_intake_mail_uid_sha
Revises: 0276_kassa_stuck_cash_threshold
"""

from __future__ import annotations

from alembic import op

revision = "0277_tax_intake_mail_uid_sha"
down_revision = "0276_kassa_stuck_cash_threshold"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_tax_document_intake_sha", "tax_document_intake", type_="unique")
    op.create_unique_constraint(
        "uq_tax_document_intake_mail_uid_sha",
        "tax_document_intake",
        ["mailbox", "message_uid", "attachment_sha256"],
    )


def downgrade() -> None:
    # Если после upgrade один и тот же шаблон пришёл несколькими письмами, старое
    # ограничение восстановить нельзя без удаления документов. Откат должен
    # остановиться с явной ошибкой, сохранив данные.
    op.create_unique_constraint(
        "uq_tax_document_intake_sha",
        "tax_document_intake",
        ["attachment_sha256"],
    )
    op.drop_constraint(
        "uq_tax_document_intake_mail_uid_sha", "tax_document_intake", type_="unique"
    )

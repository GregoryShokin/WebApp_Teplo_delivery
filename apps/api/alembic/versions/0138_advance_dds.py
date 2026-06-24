"""Выдача авансов/займов сотрудникам в ДДС.

Выдача аванса/займа — реальный отток денег, поэтому при выдаче заводится проводка ДДС
(``source_kind='salary_advance'``). Добавляем выбранный счёт-источник
(``salary_advance.wallet_id``), активируем статью «Авансы сотрудникам» (была выключена) и
заводим отдельную статью «Выдача займов сотрудникам». Гашение (удержание из ЗП) в ДДС не
проводится — деньги остаются в бизнесе.

Revision ID: 0138_advance_dds
Revises: 0137_supplier_invoice_tombstone
Create Date: 2026-06-24
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0138_advance_dds"
down_revision = "0137_supplier_invoice_tombstone"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Счёт-источник выдачи (ТК Черникова / Сейф / банк). Nullable: старые записи без счёта.
    op.add_column(
        "salary_advance",
        sa.Column("wallet_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_salary_advance_wallet",
        "salary_advance",
        "wallet",
        ["wallet_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Статья «Авансы сотрудникам» существовала, но была деактивирована — включаем.
    op.execute(
        "UPDATE dds_articles SET is_active = true WHERE code = 'employee_advance'"
    )
    # Отдельная статья для займов сотрудникам (у авансов и займов разная аналитика).
    op.execute(
        """
        INSERT INTO dds_articles (id, code, name, movement_type, activity_type, is_active)
        VALUES (gen_random_uuid(), 'vydacha_zaymov_sotrudnikam',
                'Выдача займов сотрудникам', 'outflow', 'operating', true)
        ON CONFLICT (code) DO UPDATE SET is_active = true
        """
    )


def downgrade() -> None:
    op.drop_constraint("fk_salary_advance_wallet", "salary_advance", type_="foreignkey")
    op.drop_column("salary_advance", "wallet_id")
    op.execute("DELETE FROM dds_articles WHERE code = 'vydacha_zaymov_sotrudnikam'")

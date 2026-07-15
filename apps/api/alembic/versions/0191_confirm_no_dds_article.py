"""Решение «у контрагента нет статьи ДДС» хранится, а не пересобирается на каждом открытии.

Revision ID: 0191_confirm_no_dds_article
Revises: 0190_supplier_service_periods
Create Date: 2026-07-16

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0191_confirm_no_dds_article"
down_revision = "0190_supplier_service_periods"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "counterparty_payable_profile",
        sa.Column(
            "confirm_no_dds_article",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Амнистия по решению владельца: у кого статьи нет — считаем, что решение принято.
    # Иначе карточки всех исторических контрагентов (заведённых до правила и автоматом
    # из iiko/почты/разбора банка) нельзя сохранить, пока человек не подтвердит вручную.
    op.execute(
        """
        UPDATE counterparty_payable_profile
           SET confirm_no_dds_article = true
         WHERE default_dds_article_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("counterparty_payable_profile", "confirm_no_dds_article")

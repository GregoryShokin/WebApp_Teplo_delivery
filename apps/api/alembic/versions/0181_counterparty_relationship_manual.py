"""Ручной замок типа отношений контрагента.

`counterparty_payable_profile.relationship_manual` = True, когда владелец явно выбрал тип
отношений в карточке контрагента. Пока флаг стоит, авто-классификация из iiko-синхронизации
(расходная/receivable накладная → barter) НЕ перебивает выбор. Иначе тестовая или разовая
расходная накладная в 7-дневном окне синхронизации бесконечно возвращала контрагенту «Бартер»
после каждой ручной правки (каждые 15 минут). Флаг ставится только при СМЕНЕ значения в карточке.

Revision ID: 0181_relationship_manual
Revises: 0180_inv_correction_new_ext
Create Date: 2026-07-12
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0181_relationship_manual"
down_revision = "0180_inv_correction_new_ext"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "counterparty_payable_profile",
        sa.Column(
            "relationship_manual",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("counterparty_payable_profile", "relationship_manual")

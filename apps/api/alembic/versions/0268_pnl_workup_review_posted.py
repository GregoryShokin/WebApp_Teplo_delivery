"""След проведения акта: создан ≠ проведён, и в ОПиУ идёт только проведённый.

РЕШЕНИЕ ВЛАДЕЛЬЦА 05.08.2026: акт списания система должна не только создавать, но и сразу
проводить. До этого проведение оставалось человеку, потому что ``post`` меняет складские
остатки боевой iiko.

ЗАЧЕМ ОТДЕЛЬНОЕ ПОЛЕ, А НЕ ОДИН ``writeoff_document_id``. Между «создан» и «проведён» два
разных сетевых вызова, и упасть может именно второй: документ уже есть, но лежит в NEW и в
расход не идёт. Одного id тут мало — по нему нельзя отличить «всё хорошо» от «половина
работы». Без этого различия повтор либо создавал бы второй документ, либо считал бы дело
сделанным при непроведённом акте, а расход молча не попадал бы в прибыль.

Revision ID: 0268_pnl_workup_posted
Revises: 0267_pnl_workup_review
Create Date: 2026-08-05
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0268_pnl_workup_posted"
down_revision = "0267_pnl_workup_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pnl_workup_review",
        sa.Column(
            "writeoff_posted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Акты, созданные до этой миграции, проведёнными не считаем: их проводил человек, и
    # приложение об этом не знает. Пометить их true значило бы соврать в пользу «всё готово».


def downgrade() -> None:
    op.drop_column("pnl_workup_review", "writeoff_posted")

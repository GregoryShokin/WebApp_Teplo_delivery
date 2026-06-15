"""position: признак gets_admin_oklad (курьер-окладник)

Должность может, помимо своего архетипа, получать админ-оклад в полумесячной
ведомости. Используется для «Старший курьер» (archetype=courier остаётся —
курьерский контур цел, — но добавляется админ-оклад).

Revision ID: 0109_position_admin_oklad
Revises: 0108_advance_recovery_start
Create Date: 2026-06-15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0109_position_admin_oklad"
down_revision = "0108_advance_recovery_start"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "position",
        sa.Column(
            "gets_admin_oklad",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute("UPDATE position SET gets_admin_oklad = true WHERE name = 'Старший курьер'")


def downgrade() -> None:
    op.drop_column("position", "gets_admin_oklad")

"""merge heads: cp barter settlement + position registry

Две параллельные ветки от 0099 (бартер-зачёт и реестр должностей) дали две головы —
этот пустой merge сводит их в одну линию. Никакой схемы не меняет.

Revision ID: 0101_merge_barter_position
Revises: 0100_cp_barter_settlement, 0100_position_registry
Create Date: 2026-06-12
"""

from __future__ import annotations

revision = "0101_merge_barter_position"
down_revision = ("0100_cp_barter_settlement", "0100_position_registry")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

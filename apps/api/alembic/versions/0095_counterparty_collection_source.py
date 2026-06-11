"""counterparty collection sources

Источники сбора счетов per-контрагент (iiko / email / telegram / manual). Также
routing-индекс: идентификатор канала (email-адрес, telegram-хэндл, iiko GUID)
глобально уникален → входящий документ привязывается к одному контрагенту.

Revision ID: 0095_counterparty_collection_source
Revises: 0094_counterparty_ledger_seed
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0095_cp_collection_source"
down_revision = "0094_counterparty_ledger_seed"
branch_labels = None
depends_on = None

_TABLE = "counterparty_collection_source"

KIND = postgresql.ENUM(
    "iiko", "email", "telegram", "manual", name="counterparty_collection_source_kind"
)


def upgrade() -> None:
    KIND.create(op.get_bind(), checkfirst=True)
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("counterparty_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM(name="counterparty_collection_source_kind", create_type=False),
            nullable=False,
        ),
        sa.Column("value", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["counterparty_id"], ["counterparty.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "counterparty_id", "kind", "value", name="uq_collection_source_cp_kind_value"
        ),
    )
    op.create_index("ix_collection_source_cp", _TABLE, ["counterparty_id"])
    op.create_index(
        "uq_collection_source_value_ci",
        _TABLE,
        [sa.text("lower(value)")],
        unique=True,
        postgresql_where=sa.text("value IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_collection_source_value_ci", table_name=_TABLE)
    op.drop_index("ix_collection_source_cp", table_name=_TABLE)
    op.drop_table(_TABLE)
    KIND.drop(op.get_bind(), checkfirst=True)

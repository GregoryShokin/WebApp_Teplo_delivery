"""Реестр собственников с долями: кто владеет бизнесом и в какой части.

ЗАЧЕМ ДОЛЯ. Собственников двое, по 50 % (владелец, 02.08.2026). Доля — не украшение карточки:
по ней делятся дивиденды и считается, сколько бизнес должен каждому. Без неё персональный учёт
останавливается на полпути — деньги знают, чьи они, но не знают, сколько кому причитается.

ПОЧЕМУ ОТДЕЛЬНАЯ ТАБЛИЦА, А НЕ КОЛОНКА У КОНТРАГЕНТА. Личность и расчёты собственника живут в
карточке контрагента, и это правильно: механика долга у него ровно та же, что у любого
контрагента. Но доля — свойство участия в бизнесе, а не человека: у поставщика её не бывает, и
колонка в общей таблице стояла бы пустой у всех, кроме двоих.

Revision ID: 0248_business_owner
Revises: 0247_owner_analytics
Create Date: 2026-08-02
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0248_business_owner"
down_revision = "0247_owner_analytics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "business_owner",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("counterparty_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("share_percent", sa.Numeric(5, 2), nullable=False),
        sa.Column("started_on", sa.Date(), nullable=False),
        sa.Column("ended_on", sa.Date(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "share_percent > 0 AND share_percent <= 100", name="ck_business_owner_share"
        ),
        # RESTRICT: удаление карточки не должно уносить факт владения — по нему считаются
        # дивиденды и долг бизнеса перед человеком.
        sa.ForeignKeyConstraint(["counterparty_id"], ["counterparty.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("counterparty_id", name="uq_business_owner_counterparty"),
    )
    op.create_index("ix_business_owner_counterparty", "business_owner", ["counterparty_id"])


def downgrade() -> None:
    op.drop_index("ix_business_owner_counterparty", table_name="business_owner")
    op.drop_table("business_owner")

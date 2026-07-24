"""Флаг ``dds_articles.lease_bound`` — статья-аренда помещения.

Получатель платежа по такой статье — только арендодатель из договора аренды помещения;
свободный выбор контрагента («кому платим») на ней скрыт, а помещение и арендодатель
обязательны. Подмножество ``location_required``: у коммуналки/охраны помещение есть, а
зарегистрированного арендодателя нет — там свободный контрагент остаётся.

Имена ищем как в 0211 (транслитные коды хрупки): арендные статьи из ``RENT_ARTICLE_NAMES`` —
ровно те, которым 0211 проставил ``location_required``.

Revision ID: 0214_dds_article_lease_bound
Revises: 0213_object_articles_location
Create Date: 2026-07-24
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0214_dds_article_lease_bound"
down_revision = "0213_object_articles_location"
branch_labels = None
depends_on = None

# Те же имена, что в 0211: аренда помещений — единственные статьи, где получатель обязан быть
# зарегистрированным арендодателем помещения.
RENT_ARTICLE_NAMES = ("Аренда торговых точек", "Аренда склада", "Аренда офиса")


def upgrade() -> None:
    op.add_column(
        "dds_articles",
        sa.Column(
            "lease_bound",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    names = ", ".join("'" + name.replace("'", "''") + "'" for name in RENT_ARTICLE_NAMES)
    op.execute(f"update dds_articles set lease_bound = true where name in ({names})")


def downgrade() -> None:
    op.drop_column("dds_articles", "lease_bound")

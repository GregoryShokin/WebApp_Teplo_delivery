"""Коммуналка и содержание точек тоже требуют помещение.

Решение владельца 23.07.2026: аналитика по помещению нужна не только аренде. Коммунальные
платежи и содержание торговых точек — расходы КОНКРЕТНОГО объекта, и без помещения нельзя
ответить, сколько стоит точка, ради чего контур и строился.

Ставим флаг по названию, как и аренде в 0211: коды в каталоге сгенерированы транслитом
(``kommunalnye_platezhi_0b0b4c``), и опечатка в коде тихо оставила бы статью без флага.

Исторические проводки этих статей помещения не имеют — флаг обязателен только для НОВЫХ
записей, гард стоит на входах, а не в БД.

Revision ID: 0213_object_articles_location
Revises: 0212_lease_accruals
Create Date: 2026-07-23
"""

from __future__ import annotations

from alembic import op

revision = "0213_object_articles_location"
down_revision = "0212_lease_accruals"
branch_labels = None
depends_on = None

OBJECT_ARTICLE_NAMES = ("Коммунальные платежи", "Содержание торговых точек")


def upgrade() -> None:
    names = ", ".join("'" + name.replace("'", "''") + "'" for name in OBJECT_ARTICLE_NAMES)
    op.execute(f"update dds_articles set location_required = true where name in ({names})")


def downgrade() -> None:
    names = ", ".join("'" + name.replace("'", "''") + "'" for name in OBJECT_ARTICLE_NAMES)
    op.execute(f"update dds_articles set location_required = false where name in ({names})")

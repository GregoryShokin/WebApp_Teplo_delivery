"""Собственник как ось аналитики: движение по «его» статьям обязано называть, чьё оно.

ЗАЧЕМ. Собственников двое — Павел и Григорий (решение владельца 02.08.2026), и у каждого свои
расчёты с бизнесом: он вносит деньги, получает возвраты и дивиденды. Пока проводка не называет
имени, все три статьи — общий котёл: сколько внёс каждый и сколько бизнес должен каждому, из
него не вынуть никакими отчётами.

ПОЧЕМУ ФЛАГ, А НЕ СПИСОК КОДОВ. Ровно как у ``location_required``: каталог статей правит
владелец, и захардкоженный список кодов разошёлся бы с ним при первой же новой статье.

ПОЧЕМУ У ПРОВОДКИ НЕ ПОЯВЛЯЕТСЯ КОЛОНКА. Собственник — обычный контрагент с ролью ``owner``
(значение в enum есть с самого начала и до сих пор не использовалось). Значит «кто» уже лежит
в ``counterparty_id``, а вместе с ним каждому собственнику достаются готовые ДЗ/КЗ и карточка
расчётов — то, ради чего контур и заводится.

Revision ID: 0247_owner_analytics
Revises: 0246_utility_charges
Create Date: 2026-08-02
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

revision = "0247_owner_analytics"
down_revision = "0246_utility_charges"
branch_labels = None
depends_on = None

# Статьи собственника из каталога 0114. Ищем по КОДУ: эти три заведены миграцией, и коды у них
# чистые (в отличие от статей, добавленных владельцем через интерфейс — там транслит со
# случайным хвостом). Имя как запасной ключ на случай, если код на проде правили руками.
OWNER_ARTICLES: tuple[tuple[str, str], ...] = (
    ("postuplenie_deneg_ot_sobstvennikov", "Поступление денег от собственников"),
    ("vozvrat_deneg_sobstvennikam", "Возврат денег собственникам"),
    ("dividendy", "Дивиденды"),
)


def upgrade() -> None:
    op.add_column(
        "dds_articles",
        sa.Column(
            "owner_required", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    conn = op.get_bind()
    for code, name in OWNER_ARTICLES:
        conn.execute(
            text(
                "UPDATE dds_articles SET owner_required = true "
                "WHERE code = :code OR name = :name"
            ),
            {"code": code, "name": name},
        )


def downgrade() -> None:
    op.drop_column("dds_articles", "owner_required")

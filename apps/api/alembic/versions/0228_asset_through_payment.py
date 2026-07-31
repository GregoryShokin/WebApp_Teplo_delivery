"""Основное средство доезжает от «Нового платежа» до проводки — через черновик и резерв Сейфа.

Окно «Новый платёж» — главный вход покупки оборудования, и объект в нём указать было НЕЧЕМ.
Дело не в недосмотре формы: окно не создаёт проводку. Оно создаёт черновик, деньги уходят
позже, и цепочка выглядит так:

    строка черновика  →  резерв Сейфа  →  оплата резерва  →  проводка ДДС

Привязать объект к проводке, которой ещё нет, невозможно, поэтому намерение приходится вести
через все три звена — ровно так же, как уже ведётся помещение (``location_id`` появился на этих
же таблицах в ``0211``). Отсюда две одинаковые колонки, а не одна.

ПОЧЕМУ НЕ ПРОЩЕ. Соблазн был потребовать объект не здесь, а потом — при разборе проводки,
когда она уже родилась. Но к этому моменту человек, который знает, ЧТО купили, давно ушёл:
платёж создаёт один, а разбирает выписку другой и через неделю. Именно так покупки и уходили
в расход мимо баланса.

``ondelete='SET NULL'``: карточку ОС удалять нельзя (объект с историей переводится в статус), но
если она всё же исчезнет, черновик и резерв обязаны пережить это без каскада — деньги важнее
аналитики.

Revision ID: 0228_asset_through_payment
Revises: 0227_capital_repair_floor
Create Date: 2026-07-31
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0228_asset_through_payment"
down_revision = "0227_capital_repair_floor"
branch_labels = None
depends_on = None

_TABLES = ("expense_draft_line", "safe_allocations")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            f"fk_{table}_asset_id_fixed_asset",
            table,
            "fixed_asset",
            ["asset_id"],
            ["id"],
            ondelete="SET NULL",
        )
        # Индекс частичный: объект есть у считанных строк (покупки и ремонты ОС), а таблицы
        # растут на каждом платеже. Полный индекс тут был бы платой за пустоту.
        op.create_index(
            f"ix_{table}_asset",
            table,
            ["asset_id"],
            postgresql_where=sa.text("asset_id IS NOT NULL"),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_index(f"ix_{table}_asset", table_name=table)
        op.drop_constraint(f"fk_{table}_asset_id_fixed_asset", table, type_="foreignkey")
        op.drop_column(table, "asset_id")

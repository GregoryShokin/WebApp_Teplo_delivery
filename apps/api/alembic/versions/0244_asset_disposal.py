"""Выбытие основного средства: списание с убытком вместо переоценки в ноль.

ЗАЧЕМ. Модуль ОС умел только менять стоимость объекта, который остаётся на балансе. Событие
«объекта больше нет» — кража, утрата, уничтожение — не выражалось ничем: статус ``disposed``
останавливал амортизацию и убирал карточку из строк баланса, но остаточная стоимость висела в
карточке, а убыток не появлялся нигде. Живой случай 2026-08-02: менеджер написал про уличную
скамью «украли», модель верно ответила «остаточная стоимость полностью утрачена» — и владельцу
не на что было нажать.

ПОЧЕМУ НЕ ПЕРЕОЦЕНКА В НОЛЬ. Применение предложения модели считает новую первоначальную
стоимость как «предложенная плюс накопленный износ». Для нуля это значит переписать
первоначальную в размер износа — карточка соврала бы о том, за сколько объект куплен, и акт
списания стало бы не из чего собрать. Выбытие ничего в карточке не переписывает.

ПОЧЕМУ БЕЗ НОВОЙ ТАБЛИЦЫ. ``asset_movement`` заведена вместе с модулем и содержит тип
``writeoff``, но за два месяца в неё не написал никто: журнал жизни объекта существовал только
в схеме. Списание пишется в него, а не в новую таблицу рядом — два журнала одной жизни объекта
разошлись бы при первой же правке.

Revision ID: 0244_asset_disposal
Revises: 0243_accrual_location
Create Date: 2026-08-02
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0244_asset_disposal"
down_revision = "0243_accrual_location"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Статус до списания: отмена ошибочного выбытия обязана вернуть карточку туда, где она
    # была. «Не работает» и «в работе» — разные строки баланса.
    op.add_column("asset_movement", sa.Column("previous_status", sa.String(16), nullable=True))
    # Из какого сообщения о состоянии выросло решение. NULL — списали кнопкой в карточке.
    op.add_column(
        "asset_movement", sa.Column("condition_report_id", UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_asset_movement_condition_report",
        "asset_movement",
        "asset_condition_report",
        ["condition_report_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Выбытие у объекта одно. Списать дважды — дважды показать убыток в ОПиУ.
    op.create_index(
        "uq_asset_movement_writeoff",
        "asset_movement",
        ["asset_id"],
        unique=True,
        postgresql_where=sa.text("movement_type = 'writeoff'"),
    )

    # Предложение модели «списать», а не «переоценить». Отдельный флаг, потому что это разные
    # действия: у переоценки предмет — сумма, у выбытия — сам факт, что объекта нет.
    op.add_column(
        "asset_condition_report",
        sa.Column(
            "proposed_disposal",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("asset_condition_report", "proposed_disposal")
    op.drop_index("uq_asset_movement_writeoff", table_name="asset_movement")
    op.drop_constraint("fk_asset_movement_condition_report", "asset_movement", type_="foreignkey")
    op.drop_column("asset_movement", "condition_report_id")
    op.drop_column("asset_movement", "previous_status")

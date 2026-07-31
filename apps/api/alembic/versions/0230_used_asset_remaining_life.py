"""Оценка б/у покупки предлагает ОСТАТОЧНЫЙ СРОК, а не скидку с цены.

ЧТО СЛОМАНО. Купленный с рук пароконвектомат 2018 года вставал на баланс со сроком службы
НОВОГО — 84 месяца из категории. Износ в цене уже сидит (за б/у платят меньше), а в сроке нет,
и объект семь лет амортизировался бы так, будто его только что привезли с завода. Ошибка не
видна ни в одной цифре на экране: сумма верна, категория верна, дата верна.

Нашла её сама модель на живом прогоне 31.07.2026 — оценивая состояние, она приписала к
обоснованию: «карточка показывает объект как новый (0% износа, 0 мес в эксплуатации), что не
соответствует факту покупки б/у оборудования 2018 года».

ПОЧЕМУ ДВА ВИДА ОБРАЩЕНИЙ. Существующий контур оценки отвечает на вопрос «сколько объект теперь
стоит» — это правильный вопрос про ПОЛОМКУ у объекта, который у нас уже работает. Для покупки
он неверен вдвойне: скидывать цену сразу после покупки значит посчитать износ дважды (один раз
его учёл продавец, второй раз — мы). Поэтому ``kind`` разводит два разговора:

* ``incident``  — менеджер пишет, что сломалось. Модель даёт долю потери СТОИМОСТИ (как было);
* ``purchase``  — купили б/у. Модель даёт долю израсходованного СРОКА, денег не касается вовсе.

Остаток в месяцах считает КОД: модель отдаёт долю от 0 до 1, как и в оценке стоимости. Правило
проекта «модель интерпретирует, а не вычисляет» тут то же самое — просто вместо рублей месяцы.

Умолчание ``incident`` не косметика: все записи, сделанные до этой миграции, — сообщения
менеджеров о поломках, и они должны остаться собой.

Revision ID: 0230_used_asset_remaining_life
Revises: 0229_asset_spec_profile
Create Date: 2026-07-31
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0230_used_asset_remaining_life"
down_revision = "0229_asset_spec_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "asset_condition_report",
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'incident'"),
        ),
    )
    op.create_check_constraint(
        "ck_asset_condition_report_kind",
        "asset_condition_report",
        "kind IN ('purchase','incident')",
    )
    op.add_column(
        "asset_condition_report",
        sa.Column("proposed_useful_life_months", sa.Integer(), nullable=True),
    )
    # Ноль месяцев — не «сразу списать», а ошибка разбора: объект, купленный за деньги, хоть
    # сколько-то ещё проработает. Такое предложение до владельца доходить не должно.
    op.create_check_constraint(
        "ck_asset_condition_report_life_positive",
        "asset_condition_report",
        "proposed_useful_life_months IS NULL OR proposed_useful_life_months > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_asset_condition_report_life_positive", "asset_condition_report", type_="check"
    )
    op.drop_column("asset_condition_report", "proposed_useful_life_months")
    op.drop_constraint("ck_asset_condition_report_kind", "asset_condition_report", type_="check")
    op.drop_column("asset_condition_report", "kind")

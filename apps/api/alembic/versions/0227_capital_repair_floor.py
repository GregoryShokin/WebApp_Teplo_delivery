"""Абсолютный пол капитального ремонта: 5 000 ₽ (решение владельца 2026-07-30).

Правило «ремонт или модернизация» до сих пор жило на одной доле — 15% от первоначальной
стоимости. Доля измеряет расход ОТНОСИТЕЛЬНО объекта, а капитальность работ величина
абсолютная, и на дешёвом имуществе одна доля даёт нелепицу: у стула за 1 200 ₽ ремонт за
300 ₽ — это 25%, то есть «модернизация».

Пол чинит это в паре со статьёй. Две ремонтные статьи ДДС различаются не названием, а
разделом: «Ремонт ОС» — ``investing``, «Ремонт оборудования» — ``operating``. Расход, который
стоимость объекта не увеличит, по инвестиционной статье проводить нельзя — деньги ушли бы в
инвестиции, а затрата осталась бы в расходах периода. Гейт теперь такой расход отклоняет, и
именно пол задаёт нижнюю границу того, что вообще может быть капитальным ремонтом.

ПОЧЕМУ НЕ ПРОСТО КРУГЛАЯ СУММА ВМЕСТО ДОЛИ. Соблазн был развести статьи одним порогом
(«дороже 5 000 ₽ — Ремонт ОС, дешевле — Ремонт оборудования») и не считать долю вовсе. На
нашем парке это разошлось бы с последствием: у 131 карточки из 149 в реестре инвентаризации
2026 доля в 15% меньше 5 000 ₽. Ремонт микроволновки за 12 000 ₽ на 4 000 ₽ — это 33%,
настоящий капитальный ремонт, который плоский порог запретил бы провести правильно. Поэтому
разделитель остаётся долевым, а пол только отсекает мелочь снизу.

Значение живёт в ``app_setting`` рядом с двумя другими порогами модуля: владелец правит его
сам на странице «Настройки» → «Учёт ОС». Константа ``CAPITAL_REPAIR_FLOOR`` в модели — только
умолчание на случай, когда настройки нет (тесты, пустая база), и держит ту же цифру.

Ключ помечается критическим не здесь: ``settings_service.CRITICAL_SETTING_KEYS`` — код, а не
данные, и правится в том же коммите.

Revision ID: 0227_capital_repair_floor
Revises: 0226_asset_balance_snapshot
Create Date: 2026-07-30
"""

from __future__ import annotations

import json
import uuid

import sqlalchemy as sa

from alembic import op

revision = "0227_capital_repair_floor"
down_revision = "0226_asset_balance_snapshot"
branch_labels = None
depends_on = None

SETTING_KEY = "fixed_assets.capital_repair_floor_rub"


def upgrade() -> None:
    conn = op.get_bind()
    # ``on conflict do nothing``: настройку мог завести владелец руками через интерфейс, и
    # перетирать его цифру миграция не должна.
    conn.execute(
        sa.text(
            """
            insert into app_setting (
                id, key, value, value_type, category, description,
                display_name, widget_type, widget_options, unit
            )
            values (
                :id, :key, to_jsonb(5000::int), 'money', 'Учёт ОС',
                'Сумма, дешевле которой работы не капитализируются ни при какой доле.',
                'Пол капитального ремонта', 'number', cast(:options as jsonb), '₽'
            )
            on conflict (key) do nothing
            """
        ),
        {
            "id": uuid.uuid4(),
            "key": SETTING_KEY,
            "options": json.dumps({"min": 0, "step": 100}),
        },
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "delete from app_setting_history where setting_id in "
            "(select id from app_setting where key = :key)"
        ),
        {"key": SETTING_KEY},
    )
    conn.execute(sa.text("delete from app_setting where key = :key"), {"key": SETTING_KEY})

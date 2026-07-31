"""Мойщицы: цена смены вместо месячного пула (решение владельца 2026-07-31).

Пул платил не за работу, а за календарь. 15 000 ₽/мес делились пополам между полумесячными
ведомостями, половина делилась на календарные дни периода — и получалась ставка смены:
500 ₽ в полупериоде 1–15, 468,75 ₽ в полупериоде 16–31. Одна и та же смена стоила разного
в зависимости от длины месяца, а сумма к выплате менялась от того, сколько дней в периоде,
а не сколько мойщица отработала.

Теперь цена смены задаётся прямо: (смены в полупериоде) × ставку. Стартовое значение —
500 ₽, ровно та ставка, что выходила из пула в 15-дневном полупериоде, поэтому выплаты
первой половины месяца не меняются. Владелец правит ставку в «Исходных данных → Оклады
администрации».

Старый ключ ``payroll.dishwasher_pool`` удаляется: код его больше не читает, а оставленная
ручка в «Настройках» выглядела бы рабочей и молча ни на что не влияла. Его значение не
конвертируется — точного эквивалента у пула нет (ставка зависела от длины периода), и
владелец задал новую цифру явно.

Revision ID: 0228_dishwasher_shift_rate
Revises: 0227_capital_repair_floor
Create Date: 2026-07-31
"""

from __future__ import annotations

import json
import uuid

import sqlalchemy as sa

from alembic import op

revision = "0228_dishwasher_shift_rate"
down_revision = "0227_capital_repair_floor"
branch_labels = None
depends_on = None

RATE_KEY = "payroll.dishwasher_shift_rate"
POOL_KEY = "payroll.dishwasher_pool"


def _delete_setting(conn, key: str) -> None:
    conn.execute(
        sa.text(
            "delete from app_setting_history where setting_id in "
            "(select id from app_setting where key = :key)"
        ),
        {"key": key},
    )
    conn.execute(sa.text("delete from app_setting where key = :key"), {"key": key})


def upgrade() -> None:
    conn = op.get_bind()
    # ``on conflict do nothing``: ставку мог завести владелец руками до наката.
    conn.execute(
        sa.text(
            """
            insert into app_setting (
                id, key, value, value_type, category, description,
                display_name, widget_type, widget_options, unit
            )
            values (
                :id, :key, to_jsonb(500::int), 'decimal', 'payroll',
                'Цена одной смены мойщицы: строка ведомости = смены за полупериод × ставку.',
                'Ставка мойщицы за смену, ₽', 'number', cast(:options as jsonb), '₽'
            )
            on conflict (key) do nothing
            """
        ),
        {
            "id": uuid.uuid4(),
            "key": RATE_KEY,
            "options": json.dumps({"min": 0, "step": 50}),
        },
    )
    _delete_setting(conn, POOL_KEY)


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            insert into app_setting (
                id, key, value, value_type, category, description,
                display_name, widget_type, widget_options, unit
            )
            values (
                :id, :key, to_jsonb(15000::int), 'decimal', 'payroll', null,
                'Пул мойщиц, ₽/мес', 'json', null, '₽'
            )
            on conflict (key) do nothing
            """
        ),
        {"id": uuid.uuid4(), "key": POOL_KEY},
    )
    _delete_setting(conn, RATE_KEY)

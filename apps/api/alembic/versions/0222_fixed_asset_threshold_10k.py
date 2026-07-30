"""Порог признания основного средства: 5 000 → 10 000 ₽ (решение владельца 2026-07-30).

Порог существовал в проекте в двух несогласованных видах, и обе цифры были разными.
Настройка ``fixed_assets.capitalization_threshold_rub`` (засеяна ``0002``, виджет на странице
«Настройки» из ``0005``) держала 5 000 ₽. Описания статей ДДС из каталога ``0114`` при этом
говорили менеджеру про 10 т.р. — и «Покупка ОС» («дороже 10 т.р.»), и «Содержание торговых
точек» («покупка оборудования дешевле 10 т.р.»). Покупки в вилке между двумя цифрами попадали
одновременно в операционный расход по описанию статьи и в основные средства по методологии.

Владелец 2026-07-30 разрешил расхождение в пользу 10 000 ₽: «5 000 не большая сумма, её не
нужно амортизировать». То есть верными оказались описания статей, а не настройка.

Порог ВКЛЮЧИТЕЛЬНЫЙ — ровно 10 000 ₽ это уже основное средство, поэтому описание «Покупки ОС»
уточняется до «от 10 000 ₽»: «дороже 10 т.р.» читается как строгое сравнение и отправило бы
объект ценой ровно 10 000 ₽ в расход. Описание «Содержания торговых точек» («дешевле 10 т.р.»)
остаётся как есть — оно и означает вторую сторону той же границы.

Порог применяется к БУДУЩИМ покупкам через контур «Покупка ОС». К первичной загрузке реестра
инвентаризации 2026 он не применяется: имущество уже оценено и ставится на баланс целиком —
отдельное решение владельца от той же даты.

Значение живёт в ``app_setting``, а не в константе кода: владелец правит его сам на странице
«Настройки», и ключ помечен критическим в ``settings_service.CRITICAL_SETTING_KEYS``.
Константа ``FIXED_ASSET_THRESHOLD`` в модели остаётся значением по умолчанию на случай, когда
настройка недоступна (тесты, пустая база), и приводится к той же цифре, чтобы два источника
не разъезжались снова.

Revision ID: 0222_fixed_asset_threshold_10k
Revises: 0221_fixed_assets_foundation
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0222_fixed_asset_threshold_10k"
down_revision = "0221_fixed_assets_foundation"
branch_labels = None
depends_on = None

_ARTICLE_DESCRIPTION_OLD = "Покупка мебели, техники (дороже 10 т.р.)"
_ARTICLE_DESCRIPTION_NEW = "Покупка мебели, техники (от 10 000 ₽)"


def upgrade() -> None:
    conn = op.get_bind()
    # Обновляем ТОЛЬКО если там всё ещё 5 000: если владелец уже поставил свою цифру через
    # интерфейс, миграция не должна её перетирать.
    conn.execute(
        sa.text(
            """
            update app_setting
               set value = to_jsonb(10000::int)
             where key = 'fixed_assets.capitalization_threshold_rub'
               and value = to_jsonb(5000::int)
            """
        )
    )
    conn.execute(
        sa.text(
            """
            update dds_articles
               set description = :new
             where name in ('Покупка ОС', 'Покупка основных средств')
               and description = :old
            """
        ),
        {"old": _ARTICLE_DESCRIPTION_OLD, "new": _ARTICLE_DESCRIPTION_NEW},
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            update app_setting
               set value = to_jsonb(5000::int)
             where key = 'fixed_assets.capitalization_threshold_rub'
               and value = to_jsonb(10000::int)
            """
        )
    )
    conn.execute(
        sa.text(
            """
            update dds_articles
               set description = :old
             where name in ('Покупка ОС', 'Покупка основных средств')
               and description = :new
            """
        ),
        {"old": _ARTICLE_DESCRIPTION_OLD, "new": _ARTICLE_DESCRIPTION_NEW},
    )

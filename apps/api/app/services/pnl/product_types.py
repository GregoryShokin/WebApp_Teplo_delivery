"""Какая номенклатура iiko вообще допускается в товарный учёт ОПиУ.

СКЛАДСКОЙ УЧЁТ ВЕДЁТСЯ ТОЛЬКО ПО ТОВАРАМ — правило владельца 08.08.2026. В справочнике iiko
пять типов (``GOODS``, ``DISH``, ``PREPARED``, ``MODIFIER``, ``SERVICE``), и на 08.08.2026 в
складские остатки попадали все, кроме услуг: 342 товара, 473 блюда, 290 заготовок, 179
модификаторов. Блюдо и заготовка на складе — это не закупка, а уже приготовленное из товаров;
беря их в товарный контур, отчёт считает одно и то же сырьё дважды. На 31.07.2026 остаток
складов был 1 309 155,05 ₽ при 1 219 472,81 ₽ по одним товарам — 89 682,24 ₽ разницы это
заготовки (58 605,77), блюда (21 620,44) и модификаторы (9 456,03).

ФИЛЬТР СФОРМУЛИРОВАН ОТ ОБРАТНОГО: отсекается ЯВНО не-товарный тип, а не «оставляем только
GOODS». Разница видна на GUID, которого в справочнике ещё нет: синхронизация номенклатуры и
выгрузка накладных приходят разными задачами, и товар, купленный до первого синка справочника,
на несколько часов оказывается неизвестным. Правило «оставляем только GOODS» выбросило бы его
из очереди разметки молча — ровно то, против чего построен реестр наблюдений. Правило «убираем
известные не-товары» оставляет его владельцу с суммой на руках.

КОМБО-НАБОРОВ ОТДЕЛЬНЫМ ТИПОМ НЕ БЫВАЕТ. В ``iiko_product`` их значения нет, и мы не тянем
сущность ``Combo`` из iiko вовсе — синхронизируется только ``/v2/entities/products/list``.
Наборы приходят обычными позициями меню: на 08.08.2026 в справочнике 91 «набор/сет/комбо» с
типом ``DISH``, 21 с ``PREPARED`` и 6 с ``MODIFIER``, и ни одного с ``GOODS``. То есть их
отсекает тот же фильтр, но не потому, что комбо распознано, — а потому, что комбо по своей
природе не бывает закупаемым товаром.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IikoProduct
from app.services.pnl.revision_products import normalize_iiko_product_guid

#: Единственный тип iiko, который может участвовать в складском и товарном учёте ОПиУ.
GOODS_TYPE = "GOODS"

#: Подписи типов для сообщений владельцу: «Палочки — это блюдо», а не «type=DISH».
TYPE_LABELS = {
    "DISH": "блюдо",
    "PREPARED": "заготовка",
    "MODIFIER": "модификатор",
    "SERVICE": "услуга",
    GOODS_TYPE: "товар",
}


def type_label(product_type: str | None) -> str:
    """Человеческое название типа iiko; неизвестное значение возвращаем как есть."""
    if not product_type:
        return "неизвестный тип"
    return TYPE_LABELS.get(product_type.upper(), product_type)


async def load_non_goods_guids(session: AsyncSession) -> set[str]:
    """Нормализованные GUID номенклатуры, которой в товарном учёте не место.

    Удалённые из iiko позиции тоже здесь: тип у них не меняется, а исторический GUID может
    лежать и в правиле разметки, и в наблюдении прошлого месяца.
    """
    rows = (
        await session.execute(select(IikoProduct.iiko_id).where(IikoProduct.type != GOODS_TYPE))
    ).scalars()
    return {guid for value in rows if (guid := normalize_iiko_product_guid(value))}


async def load_product_types(
    session: AsyncSession,
    guids: Iterable[str],
) -> dict[str, str]:
    """Нормализованный GUID → тип iiko. GUID вне справочника в ответе отсутствует."""
    wanted = {guid for value in guids if (guid := normalize_iiko_product_guid(value))}
    if not wanted:
        return {}
    rows = (
        await session.execute(
            select(IikoProduct.iiko_id, IikoProduct.type).where(
                func.lower(IikoProduct.iiko_id).in_(wanted)
            )
        )
    ).all()
    return {
        guid: product_type
        for iiko_id, product_type in rows
        if (guid := normalize_iiko_product_guid(iiko_id)) in wanted
    }

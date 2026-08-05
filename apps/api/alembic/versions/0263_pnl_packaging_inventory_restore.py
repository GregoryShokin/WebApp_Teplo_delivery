"""Вернуть строки инвентаризации упаковки: складской оборот их не заменяет.

ЧТО ПРОИЗОШЛО. Миграция 0260 выключила «Результаты инвентаризации упаковки» и «…коробки
д/пиццы», объявив заменой складской roll-forward «остаток на начало + приходы − остаток на
конец». Замены не вышло, потому что величины разные:

* roll-forward = РАСХОД ЗА ПЕРИОД. Для складского товара это ровно то, что считает фудкост;
  отдельной строкой ОПиУ он идти не может — будет задвоение. Владелец сформулировал это
  04.08.2026: «это по факту фудкост, это расход за весь период, а не разница между фактом
  и книгой»;
* строка эталона = РЕЗУЛЬТАТ ИНВЕНТАРИЗАЦИИ, расхождение книги с фактом. Потеря, а не
  потребление.

Итог подмены: из июля 2026 молча ушёл расход 21 018,27 ₽ (недостача упаковки 28 308,86 ₽
минус излишек коробок 7 290,59 ₽), а взамен не появилось ничего — потребление уже посчитано
фудкостом, потери перестали считаться где-либо. Здесь строки возвращаются, а roll-forward
остаётся тем, чем и был задуман, — мостом к будущему Балансу, не подключённым к ОПиУ.

ПОЧЕМУ ВОЗВРАЩАЕМ ТОЛЬКО ``line_code``, НЕ ТРОГАЯ ``include_status``. Это две независимые
характеристики товара, которые 0260 склеила в одну. ``stocked`` отвечает на вопрос «участвует
ли товар в складском контуре» и нужен балансу; ``line_code`` — «в какую строку ОПиУ идёт его
инвентаризационное расхождение». Коробка для пиццы одновременно и лежит на складе, и имеет
свою строку эталона. Поэтому статус остаётся как есть, а ссылка на строку возвращается.

ДЕВЯТЬ ПОЗИЦИЙ ОСТАЮТСЯ БЕЗ СТРОКИ НАМЕРЕННО. В сиде 0253 у них был статус
``requires_owner_review`` с примечанием «API по товару не сходится с Excel-группой; нужен
ответ владельца». Ответа не было — вопрос сняли автоматикой, переведя их в ``stocked``.
Возвращать им строку значило бы принять спорную цифру молча, поэтому ``line_code`` им не
восстанавливается: пробел должен остаться видимым. Вопрос вынесен владельцу отдельно.

Revision ID: 0263_pnl_packaging_restore
Revises: 0262_pnl_partner_commission
Create Date: 2026-08-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic.op import get_bind

revision = "0263_pnl_packaging_restore"
down_revision = "0262_pnl_partner_commission"
branch_labels = None
depends_on = None

INVENTORY_LINES = ("packaging_inventory", "pizza_box_inventory")

LINE_NOTES = {
    "packaging_inventory": (
        "iiko, результат инвентаризации упаковки по whitelist: расхождение книжного остатка "
        "с фактическим. Недостача — положительный расход, излишек уменьшает его. Складской "
        "оборот «начало + приход − конец» сюда НЕ идёт: это расход периода, он уже в "
        "фудкосте. Коробки для пиццы исключены — у них своя строка"
    ),
    "pizza_box_inventory": (
        "iiko, результат инвентаризации по четырём товарам коробок для пиццы: расхождение "
        "книжного остатка с фактическим. Недостача — положительный расход, излишек уменьшает "
        "его. Относится на направление «Пицца»"
    ),
}


def _seed_rows() -> list[tuple[str, str]]:
    """Пары «GUID → строка ОПиУ» из сида 0253 — единственного места, где эта карта осталась.

    0260 затёрла ``line_code`` безвозвратно (``SET line_code = NULL``), и её downgrade его не
    возвращает. Читаем исходный сид, а не пытаемся угадать корзину по названию товара.
    """
    import importlib.util
    from pathlib import Path

    seed_path = Path(__file__).with_name("0253_pnl_iiko_facts.py")
    spec = importlib.util.spec_from_file_location("_pnl_whitelist_seed", seed_path)
    if spec is None or spec.loader is None:  # pragma: no cover — файл лежит рядом всегда
        return []
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [
        (guid, line_code)
        for guid, line_code, status, *_rest in module.WHITELIST
        if line_code in INVENTORY_LINES and status == "include"
    ]


def upgrade() -> None:
    bind = get_bind()
    seed = _seed_rows()
    # Пустой сид означает, что файл 0253 переименовали или удалили. Молча включить две
    # пустые строки хуже, чем упасть: отчёт выглядел бы починенным при нулях в обеих.
    if not seed:
        raise RuntimeError(
            "Не удалось прочитать whitelist из 0253_pnl_iiko_facts.py — без карты "
            "«GUID → строка» восстанавливать разметку упаковки нечем"
        )
    for guid, line_code in seed:
        bind.execute(
            sa.text(
                "update pnl_product_whitelist set line_code = :line_code, updated_at = now() "
                "where iiko_product_guid = :guid and source_kind = 'inventory' "
                "and line_code is null"
            ),
            {"line_code": line_code, "guid": guid},
        )
    for code in INVENTORY_LINES:
        bind.execute(
            sa.text(
                "update pnl_line set status = 'active', source_note = :note where code = :code"
            ),
            {"note": LINE_NOTES[code], "code": code},
        )


def downgrade() -> None:
    bind = get_bind()
    bind.execute(
        sa.text(
            "update pnl_product_whitelist set line_code = null "
            "where source_kind = 'inventory' and line_code = any(:codes)"
        ),
        {"codes": list(INVENTORY_LINES)},
    )
    for code in INVENTORY_LINES:
        bind.execute(
            sa.text("update pnl_line set status = 'not_used' where code = :code"),
            {"code": code},
        )

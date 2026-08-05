"""Барная ревизия: упаковка без строки возвращается в свою, напитки получают свою.

ОТКУДА ВООБЩЕ ДВА ВИДА РЕВИЗИИ. Владелец объяснил контур 05.08.2026: ревизий на точке две.
ПОВАРСКАЯ идёт каждую неделю и считает сырьё — она заведена в модуле «Ревизии» приложения и
даёт строку ``audit_results``. БАРНАЯ идёт раз в месяц по первым числам, её проводят кассиры,
и считает она упаковку — а вместе с упаковкой диппоты и напитки, потому что стоят они на одной
барной стойке. В зеркало ОПиУ барная ревизия приходит не через модуль, а инвентаризационным
документом iiko (``/reports/storeOperations``) и питает ``packaging_inventory`` /
``pizza_box_inventory``.

ПОЧЕМУ НАПИТКИ ОКАЗАЛИСЬ «УПАКОВКОЙ». В мартовском Excel-эталоне строка 45 собрала Колу и
«Добрый» вместе с упаковкой — ровно потому, что их считают в одной барной ревизии. Разбирая
эталон, я отнёс их к ``packaging_inventory`` и пометил ``requires_owner_review``: сумма группы
по API не сходилась с Excel. Потом автоматика сняла статус, переведя их в ``stocked`` без
строки, и расхождение по ним перестало учитываться где-либо: потребление считает фудкост,
недостачу — никто.

ЧТО РЕШЕНО. Классификация идёт по ПРИРОДЕ товара, а не по тому, в какой ревизии его считают:

* упаковка барной ревизии, оставшаяся без строки, возвращается в ``packaging_inventory``.
  Владелец подтвердил спорную позицию прямо: «бутылка 0.5 — это реальная упаковка». Диппот
  Сырный идёт следом за Кисло-сладким и Чесночным, которые в этой строке с самого сида;
* напитки получают СВОЮ строку ``beverage_inventory``. Складывать их с упаковкой значит
  потерять смысл обеих: упаковка — накладной расход, напиток — товар на продажу.

КОГО НАМЕРЕННО НЕ ТРОГАЕМ. Девять позиций барной ревизии (Вилки, Ложка чайная, Шпажки, Пакет
фасовка ×2, Перчатки винил, Пищевая плёнка, Полотенца бумажные, Чековая лента ×2) уже
размечены как расход ПО ПРИХОДНОЙ НАКЛАДНОЙ — ``aux_goods`` и ``shop_maintenance``. Перевод
их в складскую строку убрал бы этот расход из отчёта, то есть изменил бы уже принятые цифры.
Противоречие «товар и закупается в расход, и пересчитывается» реальное, но снимать его —
решение владельца, а не побочный эффект миграции. Вопрос вынесен отдельно.

Revision ID: 0264_pnl_bar_audit_lines
Revises: 0263_pnl_packaging_restore
Create Date: 2026-08-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic.op import get_bind

revision = "0264_pnl_bar_audit_lines"
down_revision = "0263_pnl_packaging_restore"
branch_labels = None
depends_on = None

BEVERAGE_LINE = "beverage_inventory"
PACKAGING_LINE = "packaging_inventory"

BEVERAGE_TITLE = "Результаты инвентаризации напитков (барная ревизия)"
BEVERAGE_NOTE = (
    "iiko, результат инвентаризации напитков барной ревизии: расхождение книжного остатка "
    "с фактическим. Недостача — положительный расход, излишек уменьшает его. Напитки считают "
    "вместе с упаковкой, потому что стоят на одной стойке, но экономически это товар на "
    "продажу, а не накладной расход — поэтому строка своя. Складской оборот "
    "«начало + приход − конец» сюда НЕ идёт: это расход периода, он уже в фудкосте"
)

#: Упаковка барной ревизии, оставшаяся без строки ОПиУ. Оба GUID «Палочек» — дубль
#: номенклатуры в самом iiko; у второго суммы нулевые, но разметка нужна обоим, иначе
#: расхождение уедет мимо строки, как только по нему пройдёт движение.
PACKAGING_GUIDS = (
    ("b2ae140d-ba89-4192-b68b-3c9c012dcc14", "Бутылка 0.5"),
    ("875589b1-623a-42b6-833b-3de77b987a22", "Дип-пот Сырный"),
    ("7a7fe2eb-f8df-4d8c-9ccf-d0bdf29f483c", "Зип-лок 15/22"),
    ("831529f9-71b7-4468-b7ee-d112a710c289", 'Обертка "Рог изобилия"'),
    ("d5a959d4-9ea1-47bf-86c4-18a29896ba00", "Пакет Зип 12/17"),
    ("e77bf108-e5ae-4873-b484-997457839a48", "Палочки"),
    ("4f2130a5-0a8a-49a4-b7f8-4891b8811027", "Палочки"),
)

#: «Добрый Апельсин 1л» в барной ревизии 01.06.2026 не встретился — там был только 0.5, —
#: но это тот же напиток той же стойки. Без него остался бы пробел ровно того же рода:
#: товар складской, строки нет, недостача не считается нигде.
BEVERAGE_GUIDS = (
    ("9db111dd-fa71-45a0-a3d1-b274cd4b1508", "Кола 0.3 товар"),
    ("a17283aa-739d-427c-ad6e-cc47f772d70f", "Кола 0.5 (товар)"),
    ("95a889d6-c4cc-4a63-816a-b17a6af59700", "Кола 0.9 (товар)"),
    ("001546c0-95ce-45d7-a082-24ba851843ed", "Добрый Лимон-Лайм 1л"),
    ("470281fd-9e4c-47bd-8a81-25ab134bde4a", "Добрый Апельсин 0.5"),
    ("7c74b266-3aa6-4955-8129-3b343fd7381e", "Добрый Апельсин 1л"),
    ("b3319569-72f1-4f54-a7a6-78affb0eaa8a", "Добрый лимон-лайм ж/б"),
)

RULE_NOTE = "Барная ревизия: {kind}. Разметка по решению владельца 05.08.2026"

#: Два состояния, при которых товар НЕ трогаем.
#:
#: 1. Правило по приходным накладным со статусом ``include``: товар уже даёт расход в
#:    ``aux_goods`` / ``shop_maintenance``, и перевод в складскую строку убрал бы его из
#:    отчёта — цифры показанных месяцев поехали бы вниз без единого следа.
#: 2. ``exclude`` от ЧЕЛОВЕКА: он явно сказал «этот товар в отчёт не идёт».
#:
#: А ВОТ «СКЛАДСКОЙ БЕЗ СТРОКИ» ОТ ЧЕЛОВЕКА — НЕ ПРЕПЯТСТВИЕ, хотя выглядит как решение.
#: Ровно так размечены 13 из наших позиций: владелец прошёл по списку 04–05.08.2026 и
#: подтвердил «складской». Выбрать при этом строку он не мог — у складского источника список
#: строк в интерфейсе пуст (``GOODS_LINES_BY_SOURCE["inventory"]``), то есть его действие
#: означает «да, товар складской», а не «нет, строка ему не нужна». Считать отсутствие опции
#: отказом значило бы навсегда оставить эти товары без учёта их недостачи.
_BLOCKING_RULE = sa.text(
    """
    select 1 from pnl_product_whitelist
    where iiko_product_guid = :guid
      and (
        (source_kind = 'incoming_invoice' and include_status = 'include')
        or (include_status = 'exclude' and updated_by_user_id is not null)
      )
    """
)

# Конфликт-таргет задан КОЛОНКАМИ, а не именем: уникальность (guid, source_kind) держится
# индексом ``uq_pnl_product_whitelist_guid_source``, а не CONSTRAINT — ``on conflict on
# constraint`` его не находит, и Postgres спотыкается на выводе типов параметров.
_UPSERT = sa.text(
    """
    insert into pnl_product_whitelist
        (id, iiko_product_guid, source_kind, line_code, include_status, product_name, note,
         created_at, updated_at)
    values (gen_random_uuid(), :guid, 'inventory', :line_code, 'stocked', :name, :note,
            now(), now())
    on conflict (iiko_product_guid, source_kind) do update
        set line_code = excluded.line_code,
            include_status = 'stocked',
            product_name = coalesce(pnl_product_whitelist.product_name, excluded.product_name),
            note = excluded.note,
            updated_at = now()
    """
)


def _mark(bind, guid: str, line_code: str, name: str, note: str) -> None:
    if bind.scalar(_BLOCKING_RULE, {"guid": guid}) is not None:
        return
    bind.execute(_UPSERT, {"guid": guid, "line_code": line_code, "name": name, "note": note})


def upgrade() -> None:
    bind = get_bind()
    sort_order = bind.scalar(
        sa.text("select sort_order from pnl_line where code = :code"),
        {"code": "audit_results"},
    )
    if sort_order is None:
        raise RuntimeError(
            "Строка audit_results не найдена — не от чего отсчитать место барной ревизии"
        )
    bind.execute(
        sa.text(
            """
            insert into pnl_line
                (id, code, title, block, kind, sort_order, level, sign_role, month_basis,
                 direction_aware, status, source_note, created_at, updated_at)
            values (gen_random_uuid(), :code, :title, 'overhead', 'source', :sort_order, 1, -1,
                    'calendar', false, 'active', :note, now(), now())
            on conflict (code) do update
                set title = excluded.title,
                    status = 'active',
                    source_note = excluded.source_note,
                    updated_at = now()
            """
        ),
        {
            "code": BEVERAGE_LINE,
            "title": BEVERAGE_TITLE,
            "sort_order": sort_order + 5,
            "note": BEVERAGE_NOTE,
        },
    )
    for guid, name in PACKAGING_GUIDS:
        _mark(bind, guid, PACKAGING_LINE, name, RULE_NOTE.format(kind="упаковка"))
    for guid, name in BEVERAGE_GUIDS:
        _mark(bind, guid, BEVERAGE_LINE, name, RULE_NOTE.format(kind="напиток, товар на продажу"))


def downgrade() -> None:
    bind = get_bind()
    # Сначала снимаем ссылки на строку: line_code — внешний ключ с ondelete RESTRICT.
    bind.execute(
        sa.text(
            "update pnl_product_whitelist set line_code = null, updated_at = now() "
            "where source_kind = 'inventory' and line_code = :line_code"
        ),
        {"line_code": BEVERAGE_LINE},
    )
    bind.execute(
        sa.text(
            "update pnl_product_whitelist set line_code = null, updated_at = now() "
            "where source_kind = 'inventory' and line_code = :line_code "
            "and iiko_product_guid = any(:guids)"
        ),
        {"line_code": PACKAGING_LINE, "guids": [guid for guid, _name in PACKAGING_GUIDS]},
    )
    bind.execute(sa.text("delete from pnl_line where code = :code"), {"code": BEVERAGE_LINE})

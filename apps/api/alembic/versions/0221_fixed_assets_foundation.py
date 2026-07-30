"""Фундамент модуля ОС под первичную загрузку реестра инвентаризации 2026.

Миграция ``0201`` завела таблицы, но модуль ни разу не наполнялся: карточек в базе ноль,
справочник ``asset_category`` пуст. Перед заливкой 149 карточек по итогам фотоинвентаризации
(реестр от 2026-08-01, 2 313 150 ₽) закрываются четыре дыры, каждая из которых иначе портит
данные молча.

**Категории.** ``accrue_depreciation`` берёт СПИ из карточки, а если он пуст — из категории
(``services/fixed_assets.py:36-44``); объект без СПИ она молча ПРОПУСКАЕТ (``:84-86``).
Пустой справочник означал бы, что ночная джоба закрытия месяца отработает вхолостую и никто
не заметит: ошибки нет, начислений нет. Десять категорий и их СПИ — решение владельца
2026-07-30, те же, по которым посчитан реестр.

«Не работающее оборудование» в реестре стоит рядом с ними как тип, но категорией здесь
СОЗНАТЕЛЬНО не заводится: в модели это статус карточки (``not_working``). Заведи его
категорией со сроком — и три заведомо мёртвых объекта начнут амортизироваться.

**Инвентарный номер.** Поле было ``nullable`` без уникальности, а номера по решению владельца
генерируются автоматически при создании карточки. Генератор по образцу проекта
(``warehouse_invoices.next_invoice_number``) читает максимум и прибавляет единицу — схема
«select → пусто → insert» не защищает от гонки, два одновременных создания дают один номер.
Частичный уникальный индекс делает такую гонку ошибкой вставки, а не тихим дублем; ``NULL``
он не трогает, поэтому карточки без номера (легаси, ручной ввод) остаются возможны.

**Помещение.** ``fixed_asset.location`` — свободная строка, хотя реестр помещений (``location``)
в проекте есть и служит осью «где» для проводок ДДС (``cashflow_transactions.location_id``).
Две несвязанные записи о том же самом означают, что имущество нельзя сопоставить с расходами
по тому же помещению. Строка остаётся как есть, чтобы не терять текст из старых источников,
а рядом появляется честная ссылка.

Сюда же — помещение «Склад»: в реестре помещений его нет (``0002`` завела только «Черникова»
и «Гагарина»), а 40 позиций описи физически лежат именно там. Вставка идемпотентна по имени:
если владелец уже завёл склад через интерфейс, миграция его не тронет и не задвоит.

**Бренд и происхождение.** ``brand_model`` заполнен у 76 позиций реестра и служит опознанием
объекта при следующем обходе. ``source_ref`` («Черникова №33», «Склад №12») — след, по которому
карточка сходится с описью и фотографиями; без него после заливки нельзя доказать, откуда
взялась цифра в балансе.

Revision ID: 0221_fixed_assets_foundation
Revises: 0220_iiko_cash_payout
Create Date: 2026-07-30
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0221_fixed_assets_foundation"
down_revision = "0220_iiko_cash_payout"
branch_labels = None
depends_on = None


# Категория → СПИ в месяцах. Решение владельца 2026-07-30; те же сроки, по которым посчитан
# реестр «Реестр_ОС_и_амортизация_2026-08-01.xlsx» (лист «Методология»).
ASSET_CATEGORIES: list[tuple[str, int, str]] = [
    ("Тепловое оборудование", 84, "Печи, плиты, фритюрницы, грили, саламандры."),
    (
        "Холодильное/морозильное оборудование",
        84,
        "Шкафы, лари, столы холодильные, витрины.",
    ),
    ("Кассовое оборудование", 60, "POS-компьютеры, чековые принтеры, денежные ящики."),
    ("Оборудование торговых залов", 60, "Витрины, стойки, оборудование зоны гостя."),
    (
        "Вспомогательное оборудование",
        120,
        "Стеллажи, столы и полки из нержавеющей стали, мойки, зонты вытяжные.",
    ),
    ("Электромеханическое оборудование", 84, "Миксеры, слайсеры, мясорубки, тестораскатки."),
    ("Электроника и оргтехника", 36, "Компьютеры, МФУ, телефоны, сетевое оборудование."),
    ("Системы кондиционирования", 84, "Кондиционеры, вентиляция."),
    ("Прочий кухонный инвентарь", 36, "Кастрюли, сковороды, инвентарь длительного пользования."),
    ("Мебель и предметы интерьера", 84, "Столы, стулья, шкафы, вывески."),
]


def upgrade() -> None:
    op.add_column(
        "fixed_asset",
        sa.Column("location_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_fixed_asset_location",
        "fixed_asset",
        "location",
        ["location_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_fixed_asset_location", "fixed_asset", ["location_id"])
    op.add_column("fixed_asset", sa.Column("brand_model", sa.String(length=160), nullable=True))
    op.add_column("fixed_asset", sa.Column("source_ref", sa.String(length=64), nullable=True))

    # Частичный: карточка без номера допустима, две карточки с одним номером — нет.
    op.create_index(
        "uq_fixed_asset_inventory_number",
        "fixed_asset",
        ["inventory_number"],
        unique=True,
        postgresql_where=sa.text("inventory_number IS NOT NULL"),
    )

    conn = op.get_bind()

    # Склад в реестре помещений. Сверка по ИМЕНИ, а не по id: владелец мог завести его сам
    # через интерфейс, и тогда миграция обязана промолчать, а не задвоить помещение.
    # Организацию берём у существующих помещений, чтобы склад не уехал в чужое юрлицо.
    conn.execute(
        sa.text(
            """
            insert into location (id, organization_id, name, address, status, kind,
                                  iiko_store_ids)
            select cast(:new_id as uuid), l.organization_id, 'Склад', null,
                   cast('active' as location_status), 'warehouse', '[]'::jsonb
              from location l
             where not exists (select 1 from location where name = 'Склад')
             group by l.organization_id
             order by count(*) desc
             limit 1
            """
        ),
        {"new_id": uuid.uuid4()},
    )

    for name, life_months, note in ASSET_CATEGORIES:
        # Типы параметров проставлены явно: без каста asyncpg не может вывести тип имени,
        # которое стоит и в списке вставки, и в сравнении под NOT EXISTS.
        conn.execute(
            sa.text(
                """
                insert into asset_category (id, name, useful_life_months, note)
                select cast(:id as uuid), cast(:name as varchar),
                       cast(:life as integer), cast(:note as text)
                 where not exists (
                         select 1 from asset_category where name = cast(:name as varchar)
                       )
                """
            ),
            {"id": uuid.uuid4(), "name": name, "life": life_months, "note": note},
        )


def downgrade() -> None:
    conn = op.get_bind()
    # Категории сносим только те, на которых не висит ни одной карточки: если заливка реестра
    # уже прошла, откат схемы не должен обнулять СПИ у живых объектов.
    conn.execute(
        sa.text(
            """
            delete from asset_category
             where name = any(:names)
               and not exists (
                     select 1 from fixed_asset
                      where fixed_asset.category_id = asset_category.id
                   )
            """
        ),
        {"names": [name for name, _, _ in ASSET_CATEGORIES]},
    )
    conn.execute(
        sa.text(
            """
            delete from location
             where name = 'Склад'
               and kind = 'warehouse'
               and not exists (
                     select 1 from fixed_asset where fixed_asset.location_id = location.id
                   )
            """
        )
    )

    op.drop_index("uq_fixed_asset_inventory_number", table_name="fixed_asset")
    op.drop_column("fixed_asset", "source_ref")
    op.drop_column("fixed_asset", "brand_model")
    op.drop_index("ix_fixed_asset_location", table_name="fixed_asset")
    op.drop_constraint("fk_fixed_asset_location", "fixed_asset", type_="foreignkey")
    op.drop_column("fixed_asset", "location_id")

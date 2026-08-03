"""Зеркало месячных фактов iiko и whitelist номенклатуры для товарных строк ОПиУ.

ЗАЧЕМ ЗЕРКАЛО, А НЕ ЗАПРОС НА ЛЕТУ. iiko Server доступен только с прод-адреса, отвечает
секундами и режет частые обращения. Ходить в него при каждом открытии отчёта — значит
поставить страницу в зависимость от внешней системы и её лимитов. Ночная джоба складывает
месячные итоги сюда, а отчёт читает один SELECT. Это единственное место проектора, где
данные хранятся, и хранятся они потому, что снаружи их взять дорого, а не потому, что
считать долго.

``iiko_revenue_period`` НЕ ТРОГАЕМ ни одной колонкой. Та таблица — база налога, её сервис
удаляет строки другой гранулярности по ключу, а докстринг предупреждает о двойном счёте при
смешивании. Добавив туда направления, мы сломали бы декларацию, а не отчёт.

WHITELIST ВОССТАНОВЛЕН ИЗ GIT: research/processed/economic_block/pnl_product_whitelist.csv
удалён коммитом 716ada93 при чистке research-слоя. Без него четыре товарные строки эталона
собрать нельзя: отбор идёт по конкретным идентификаторам товаров, а не по словам в названии.
Проверка на марте 2026 показала, почему: поиск по слову «вакуум» давал 2 352 ₽ против 1 658 ₽
контрольной суммы — в выдачу попадал посторонний товар.

Четыре коробки для пиццы в исходном файле помечены ``exclude`` внутри корзины упаковки —
это не «не считать», а «считать отдельной строкой»: в эталоне у них своя строка блока прямых
постоянных, потому что они относятся только на «Пиццу». Сид переносит их в собственную
корзину, иначе строка осталась бы пустой навсегда.

Revision ID: 0253_pnl_iiko_facts
Revises: 0252_pnl_source_rules
Create Date: 2026-08-03
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0253_pnl_iiko_facts"
down_revision = "0252_pnl_source_rules"
branch_labels = None
depends_on = None


# (guid товара, корзина = код строки ОПиУ, статус, название, примечание)
WHITELIST: list[tuple[str, str, str, str, str]] = [
    (
        "359e2307-c04e-4782-a765-2b2edd13aa30",
        "shop_maintenance",
        "include",
        "Чековая лента бол. 80х80",
        "Входит в строку 15 мартовского Excel; item.sum по накладным.",
    ),
    (
        "574f3e9b-7c7b-4f44-bec5-be4ed40dc7e4",
        "shop_maintenance",
        "include",
        "Чековая лента мал. 57х40",
        "Включать как чековую ленту в будущих месяцах; в марте 2026 покупок не было.",
    ),
    (
        "05c838e1-12c6-4bf4-9485-5f88b45b83f9",
        "shop_maintenance",
        "include",
        "Туалетная бумага",
        "Входит в строку 16 мартовского Excel.",
    ),
    (
        "d8ad323b-86b1-4c52-9b82-1befd7817863",
        "shop_maintenance",
        "include",
        "Полотенца бумажные",
        "Входит в строку 17 мартовского Excel.",
    ),
    (
        "36ec61ec-f94e-4c14-b0af-55e2c092af4d",
        "shop_maintenance",
        "include",
        "Вафельное полотенце рулон",
        "Входит в строку 18 мартовского Excel.",
    ),
    (
        "69046eca-2ff9-4da5-a2d2-db72bebaa0c5",
        "shop_maintenance",
        "include",
        "Мешок для мусора 240л",
        "Один из товаров группы строки 19.",
    ),
    (
        "7e0a982f-54a7-4527-acc3-317fb80ef95d",
        "shop_maintenance",
        "include",
        "Мешок для мусора 120л",
        "Один из товаров группы строки 19.",
    ),
    (
        "7e705430-de25-43c5-add1-74fd84ae3c87",
        "shop_maintenance",
        "include",
        "Мусорный пакет 60л",
        "Один из товаров группы строки 19.",
    ),
    (
        "f7990363-d9bd-4e1c-bd81-5622fd5f7324",
        "shop_maintenance",
        "include",
        "Моющие и чистящие средства",
        "Входит в строку 20 мартовского Excel.",
    ),
    (
        "f547156c-f479-47ad-b7dc-d9223cd4c6f2",
        "shop_maintenance",
        "include",
        "Мешок кондитерский одноразовый",
        "Входит в строку 21 мартовского Excel.",
    ),
    (
        "a71a909f-6ae0-4370-8af7-09462db5ddfe",
        "aux_goods",
        "include",
        "Перчатки винил",
        "Один из товаров группы строки 3.",
    ),
    (
        "5d855c68-0488-48ea-8c0a-12f6f67ece61",
        "aux_goods",
        "include",
        "Перчатки хоз плотные",
        "Один из товаров группы строки 3.",
    ),
    (
        "5c104c03-ed14-4a21-a15c-c537d8867461",
        "aux_goods",
        "include",
        "Фольга 29см",
        "Входит в строку 4 мартовского Excel.",
    ),
    (
        "edcb9c0d-d8eb-4b0d-8447-87a4c55d7eb1",
        "aux_goods",
        "include",
        "Лента для датера",
        "Включать в будущих месяцах; в марте 2026 покупок не было.",
    ),
    (
        "041cb6b1-aaf8-418d-b732-42e763a97e80",
        "aux_goods",
        "include",
        "Пищевая пленка широкая",
        "Входит в строку 6 мартовского Excel.",
    ),
    (
        "ab45600a-5e98-4f87-bc1b-e07190b683c1",
        "aux_goods",
        "include",
        "Шпажки",
        "Входит в строку 7 мартовского Excel.",
    ),
    (
        "3fa7853d-7470-4d07-920b-bb7e7e495390",
        "aux_goods",
        "include",
        "Ложка чайная",
        "Входит в строку 8 мартовского Excel.",
    ),
    (
        "0d6bfb8a-0aeb-4cf1-8692-fa46689ec90d",
        "aux_goods",
        "include",
        "Вилки",
        "Входит в строку 9 мартовского Excel.",
    ),
    (
        "39dc419a-1e20-4a21-9405-8e105186df57",
        "aux_goods",
        "include",
        "Пакет фасовка",
        "Включать как фасовочные пакеты в будущих месяцах; в марте 2026 покупок не было.",
    ),
    (
        "be7b973b-f025-4c7b-bd7e-570aed4b958c",
        "aux_goods",
        "include",
        "Пакет фасовка 10+6",
        "Включать как фасовочные пакеты в будущих месяцах; в марте 2026 покупок не было.",
    ),
    (
        "48955f58-23ca-4ec9-a394-f504035c5df5",
        "aux_goods",
        "include",
        "Пакет вакуум 25/35",
        "Входит в строку 11 мартовского Excel.",
    ),
    (
        "762ed25a-5581-4858-82da-83d75d433b25",
        "aux_goods",
        "include",
        "Пакет вакуум 16х25",
        "Входит в строку 11 мартовского Excel.",
    ),
    (
        "c878bae1-fc67-4093-a5cf-d06d43ae321f",
        "aux_goods",
        "exclude",
        "Пакет вакуум 160/200",
        "Не входит в мартовский Excel; широкий поиск по вакуумным пакетам дает лишние 694 руб.",
    ),
    (
        "358af10e-7c6b-4ce3-b51d-ccfaa2f9d41f",
        "packaging_inventory",
        "include",
        "Ланч бокс 3 секции",
        "Один из товаров группы строки 25.",
    ),
    (
        "a3b0649f-c41f-4063-8bec-38432e9d673d",
        "packaging_inventory",
        "include",
        "Ланч-бокс 2 секции",
        "Один из товаров группы строки 25.",
    ),
    (
        "37977d9f-a43e-40f8-9798-e29c2d9dbe14",
        "packaging_inventory",
        "include",
        "Ланчбокс 1 секция",
        "Один из товаров группы строки 25.",
    ),
    (
        "46d03929-245c-4328-8196-cc6af3300be7",
        "packaging_inventory",
        "include",
        "Коробка под фри",
        "Входит в строку 26 мартовского Excel.",
    ),
    (
        "831529f9-71b7-4468-b7ee-d112a710c289",
        "packaging_inventory",
        "requires_owner_review",
        'Обертка "Рог изобилия"',
        "API по товару не сходится с Excel-группой; нужен ответ владельца, какие iiko-товары вх",
    ),
    (
        "55d7713e-df82-40be-a1b0-a51fdc337145",
        "packaging_inventory",
        "include",
        "Коробка под бургер",
        "Входит в строку 28 мартовского Excel.",
    ),
    (
        "2a83cf81-2349-4731-a639-7170fc1387ed",
        "packaging_inventory",
        "include",
        "Пакеты крафт 8х18",
        "Входит в строку 29 мартовского Excel.",
    ),
    (
        "6631a392-0944-438f-80bb-f184716e965a",
        "packaging_inventory",
        "include",
        "Пакет крафт с ручками",
        "Входит в строку 30 мартовского Excel.",
    ),
    (
        "c7ce12d8-bbf5-496b-9120-fefba85f1c34",
        "packaging_inventory",
        "include",
        "Пакет крафт без ручек",
        "Входит в строку 31 мартовского Excel.",
    ),
    (
        "1fc0e714-57ab-491b-87be-da3420854619",
        "packaging_inventory",
        "include",
        "Соусник черный открытый",
        "Входит в строку 32 мартовского Excel.",
    ),
    (
        "d2c2f2f4-93cb-42c8-a3e4-205540c9eaae",
        "packaging_inventory",
        "include",
        "Зубочистки",
        "Входит в строку 33 мартовского Excel.",
    ),
    (
        "62e4c086-38b9-4a78-abb1-859e8c62dcb0",
        "packaging_inventory",
        "include",
        "Салфетки",
        "Входит в строку 34 мартовского Excel.",
    ),
    (
        "e77bf108-e5ae-4873-b484-997457839a48",
        "packaging_inventory",
        "requires_owner_review",
        "Палочки",
        "API по товару не сходится с Excel-группой; нужен ответ владельца по составу строки 35.",
    ),
    (
        "4f2130a5-0a8a-49a4-b7f8-4891b8811027",
        "packaging_inventory",
        "requires_owner_review",
        "Палочки",
        "Дублирующее название товара; в документе 0012 сумма 0, нужен ответ владельца включать",
    ),
    (
        "b2ae140d-ba89-4192-b68b-3c9c012dcc14",
        "packaging_inventory",
        "requires_owner_review",
        "Бутылка 0.5",
        "API по товару не сходится с Excel-группой; нужен ответ владельца по составу строки 36.",
    ),
    (
        "d4f9f95b-5d39-4d0e-ad32-3f88718aa2d9",
        "packaging_inventory",
        "include",
        "Соусник 50мл",
        "Входит в строку 37 мартовского Excel.",
    ),
    (
        "0e4ea631-28f1-48f8-a4e7-559438f3e0ec",
        "packaging_inventory",
        "include",
        "Соусник бутылка 0.097",
        "Входит в строку 38 мартовского Excel.",
    ),
    (
        "8362edce-ca43-48d0-ba58-8701b289519a",
        "packaging_inventory",
        "include",
        "Контейнер для соуса 125гр",
        "Входит в строку 39 мартовского Excel.",
    ),
    (
        "ddaf413e-f030-4318-abcf-26db96b4238c",
        "packaging_inventory",
        "include",
        "Контейнер 250гр",
        "Входит в строку 40 мартовского Excel.",
    ),
    (
        "96801277-d55b-4673-9f46-e1bbf756fc5f",
        "packaging_inventory",
        "include",
        "Крышки для конт. 125/250гр",
        "Входит в строку 41 мартовского Excel.",
    ),
    (
        "b861305b-6a89-4020-bfdc-72afecff8969",
        "packaging_inventory",
        "include",
        "Леденцы",
        "Входит в строку 42 мартовского Excel.",
    ),
    (
        "d5a959d4-9ea1-47bf-86c4-18a29896ba00",
        "packaging_inventory",
        "requires_owner_review",
        "Пакет Зип 12/17",
        "API по товару не сходится с Excel-группой; нужен ответ владельца, есть ли еще зип-лок",
    ),
    (
        "79787bbf-5896-4474-a76e-dd9c22ca6008",
        "packaging_inventory",
        "include",
        "Дип-пот Кисло-сладкий",
        "Один из товаров группы строки 44.",
    ),
    (
        "5bd77cd7-53f2-41bd-8205-4d307470a2b5",
        "packaging_inventory",
        "include",
        "Дип-пот Чесночный",
        "Один из товаров группы строки 44.",
    ),
    (
        "001546c0-95ce-45d7-a082-24ba851843ed",
        "packaging_inventory",
        "requires_owner_review",
        "Добрый Лимон-Лайм 1л",
        "Один из товаров группы строки 45; сумма группы API не сходится с Excel.",
    ),
    (
        "9db111dd-fa71-45a0-a3d1-b274cd4b1508",
        "packaging_inventory",
        "requires_owner_review",
        "Кола 0.3 товар",
        "Один из товаров группы строки 45; сумма группы API не сходится с Excel.",
    ),
    (
        "a17283aa-739d-427c-ad6e-cc47f772d70f",
        "packaging_inventory",
        "requires_owner_review",
        "Кола 0.5 (товар)",
        "Один из товаров группы строки 45; сумма группы API не сходится с Excel.",
    ),
    (
        "95a889d6-c4cc-4a63-816a-b17a6af59700",
        "packaging_inventory",
        "requires_owner_review",
        "Кола 0.9 (товар)",
        "Один из товаров группы строки 45; сумма группы API не сходится с Excel.",
    ),
    (
        "15c31884-118a-4ad9-af5b-91715026671a",
        "packaging_inventory",
        "include",
        "Крышка для бутылок",
        "Входит в строку 46 мартовского Excel.",
    ),
    (
        "fbf96eb0-f369-4e8a-a639-d81488801a12",
        "packaging_inventory",
        "include",
        "Пакет майка спасибо за покупку",
        "Входит в строку 47 мартовского Excel.",
    ),
    (
        "c550cc31-9448-460b-baa7-070f47662e13",
        "pizza_box_inventory",
        "include",
        "Коробка 30х30 бренд",
        "Коробка для пиццы: своя строка эталона, из упаковки исключена",
    ),
    (
        "9ccc60ef-b4d0-43d6-a6a4-877ee1950456",
        "pizza_box_inventory",
        "include",
        "Коробка 40х40 бренд",
        "Коробка для пиццы: своя строка эталона, из упаковки исключена",
    ),
    (
        "46aeed5f-83e4-41de-8d0b-8310ed726421",
        "pizza_box_inventory",
        "include",
        "Коробка для пиццы 30х30",
        "Коробка для пиццы: своя строка эталона, из упаковки исключена",
    ),
    (
        "ad0e6fd7-2319-41a9-94c7-2136a05dc7a5",
        "pizza_box_inventory",
        "include",
        "Коробка для пиццы 40х40",
        "Коробка для пиццы: своя строка эталона, из упаковки исключена",
    ),
]


def upgrade() -> None:
    op.create_table(
        "pnl_iiko_fact",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("period_month", sa.Date, nullable=False),
        sa.Column("metric_code", sa.String(32), nullable=False),
        # Роллы / Пицца / Горячий цех / Бар либо total. Заложено сразу: включение разреза
        # по направлениям не должно требовать миграции данных.
        sa.Column("direction", sa.String(16), nullable=False, server_default=sa.text("'total'")),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("rows_count", sa.Integer, nullable=True),
        sa.Column("source_ref", sa.Text, nullable=True),
        # Прошлое значение при перезаливке месяца. Выручка закрытого месяца меняться не
        # должна — если изменилась, это видно, а не подменяется молча.
        sa.Column("previous_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column(
            "synced_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "uq_pnl_iiko_fact_slot",
        "pnl_iiko_fact",
        ["period_month", "metric_code", "direction"],
        unique=True,
    )

    op.create_table(
        "pnl_product_whitelist",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("iiko_product_guid", sa.String(64), nullable=False),
        sa.Column(
            "line_code",
            sa.String(64),
            sa.ForeignKey("pnl_line.code", ondelete="RESTRICT"),
            nullable=False,
        ),
        # include — считается; requires_owner_review — найдено расхождение с контролем,
        # в расчёт НЕ идёт, но показывается отдельной цифрой «не отнесено»; exclude —
        # исключено осознанно.
        sa.Column("include_status", sa.String(24), nullable=False),
        sa.Column("product_name", sa.String(255), nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "include_status in ('include', 'requires_owner_review', 'exclude')",
            name="ck_pnl_product_whitelist_status",
        ),
    )
    op.create_index(
        "uq_pnl_product_whitelist_guid_line",
        "pnl_product_whitelist",
        ["iiko_product_guid", "line_code"],
        unique=True,
    )

    connection = op.get_bind()
    for guid, line_code, status, name, note in WHITELIST:
        connection.execute(
            sa.text(
                """
                INSERT INTO pnl_product_whitelist
                    (id, iiko_product_guid, line_code, include_status, product_name, note)
                VALUES (gen_random_uuid(), :guid, :line_code, :status, :name, :note)
                ON CONFLICT DO NOTHING
                """
            ),
            {
                "guid": guid,
                "line_code": line_code,
                "status": status,
                "name": name,
                "note": note or None,
            },
        )


def downgrade() -> None:
    op.drop_index("uq_pnl_product_whitelist_guid_line", table_name="pnl_product_whitelist")
    op.drop_table("pnl_product_whitelist")
    op.drop_index("uq_pnl_iiko_fact_slot", table_name="pnl_iiko_fact")
    op.drop_table("pnl_iiko_fact")

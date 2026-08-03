"""Справочник строк ОПиУ: 53 статьи эталона, 20 расчётных и 3 служебных приёмника.

Эталон — управленческий ОПиУ владельца (шаблон «Нескучных финансов», лист «Статьи» плюс
месячный лист). Порядок строк, блоки, знаки и нормативные коридоры взяты оттуда дословно:
``business-docs/finance/pnl-methodology.md``, раздел «Список строк P&L из листа Статьи».

ПОЧЕМУ 73, А НЕ 53. Кроме статей-источников отчёт содержит расчётные строки каскада
(Маржинальный доход → Валовая прибыль по направлениям → Валовая прибыль → EBITDA → Чистая
прибыль) и рентабельности на каждом уровне. Они хранятся здесь же с декларативной формулой:
состав блока меняется вместе с разметкой статей, и формула, лежащая в коде, разъехалась бы
со справочником молча.

ПОЧЕМУ СТРОКИ ЗАКРЫТОЙ ТОЧКИ ВСЁ РАВНО ЗАВЕДЕНЫ. Пять строк «Гагарина» и «Печатные материалы»
получают ``status='not_used'``. Точка не работает с 2024 года, идентификаторов iiko у неё нет,
и считать её нечем. Но в таблице владельца эти строки есть, и молча выкинуть их значило бы
сделать отчёт несопоставимым с его файлом. На экране это бледный бейдж «не используется» —
никогда не ноль: пустое и ноль в управленческом учёте разные вещи.

СЛУЖЕБНЫЕ ПРИЁМНИКИ нужны уравнению сходимости. Каждый рубль оттока получает ровно один
ярлык, и «не разнесено» обязано быть нулём — иначе потеря пройдёт незамеченной. Задвоение
шумит, потеря молчит, поэтому ловим её тождеством, а не глазами.

Revision ID: 0251_pnl_lines
Revises: 0250_allocation_origin
Create Date: 2026-08-03
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0251_pnl_lines"
down_revision = "0250_allocation_origin"
branch_labels = None
depends_on = None


# (code, title, block, kind, sort, level, sign_role, month_basis, direction_aware,
#  status, formula, norm_min, norm_max, source_note)
#
# sign_role — как КАСКАД использует строку: +1 прибавляет, -1 вычитает. Величина в строке
# всегда положительная, знак живёт здесь. Исключение — компенсирующие величины (излишек
# ревизии, возврат), они приходят из источника отрицательными и уменьшают расход.
LINES: list[tuple] = [
    # ---------------------------------------------------------------- Выручка
    (
        "revenue", "Выручка", "revenue", "subtotal", 10, 0, 1, "calendar", True, "active",
        {
            "op": "sub",
            "args": [
                "revenue_net_chernikova",
                {"op": "sum", "args": ["partner_commission", "customer_refunds"]},
            ],
        },
        None, None,
        "Выручка со скидками минус комиссия партнёрам и возвраты клиентам",
    ),
    (
        "revenue_gross_gagarina", "Выручка без учета скидок Гагарина", "revenue", "memo",
        20, 1, 1, "calendar", True, "not_used", None, None, None,
        "Точка не работает с 2024 года",
    ),
    (
        "revenue_net_gagarina", "Выручка с учетом скидок Гагарина", "revenue", "source",
        30, 1, 1, "calendar", True, "not_used", None, None, None,
        "Точка не работает с 2024 года",
    ),
    (
        "revenue_gross_chernikova", "Выручка без учета скидок Черникова", "revenue", "memo",
        40, 1, 1, "calendar", True, "active", None, None, None,
        "iiko OLAP «Отчёт о выручке по направлениям», сумма без скидки. Справочно: "
        "в каскад не входит, выручкой отчёта служит строка со скидками",
    ),
    (
        "revenue_net_chernikova", "Выручка с учетом скидок Черникова", "revenue", "source",
        50, 1, 1, "calendar", True, "active", None, None, None,
        "iiko OLAP «Отчёт о выручке по направлениям», сумма со скидкой",
    ),
    (
        "partner_commission", "Комиссия партнерам (с -)", "revenue", "source",
        60, 1, -1, "calendar", True, "active", None, None, None,
        "20% от суммы со скидкой по партнёру «В гостях у Алисы», iiko OLAP «Отчёт о партнёрах»",
    ),
    (
        "customer_refunds", "Возвраты клиентам (с минусом)", "revenue", "source",
        70, 1, -1, "cash", True, "active", None, None, None,
        "ДДС, одноимённая статья",
    ),
    # ------------------------------------------------------- Производственные
    (
        "production_costs", "Производственные расходы", "production", "subtotal",
        80, 0, -1, "calendar", False, "active",
        {"op": "sum", "args": ["variable_costs", "direct_fixed", "overhead"]}, 0.700, 0.800,
        "Переменные + прямые постоянные + общепроизводственные",
    ),
    (
        "variable_costs", "Переменные", "variable", "subtotal",
        90, 0, -1, "calendar", True, "active",
        {"op": "sum", "block": "variable"}, None, None, "Фудкост",
    ),
    (
        "food_cost_chernikova", "Расход продуктов (фудкост) Черникова", "variable", "source",
        100, 1, -1, "calendar", True, "active", None, None, None,
        "iiko OLAP «Отчёт о выручке по направлениям», себестоимость",
    ),
    (
        "food_cost_gagarina", "Расход продуктов (фудкост) Гагарина", "variable", "source",
        110, 1, -1, "calendar", True, "not_used", None, None, None,
        "Точка не работает с 2024 года",
    ),
    (
        "margin_income", "Маржинальный доход", "margin", "subtotal",
        120, 0, 1, "calendar", True, "active",
        {"op": "sub", "args": ["revenue", "variable_costs"]}, None, None,
        "Выручка минус переменные расходы",
    ),
    (
        "margin_ratio", "Рентабельность по маржинальному доходу, %", "margin", "ratio",
        130, 1, 1, "calendar", False, "active",
        {"op": "ratio", "num": "margin_income", "den": "revenue"}, 0.600, 0.700, None,
    ),
    # ----------------------------------------------------- Прямые постоянные
    (
        "direct_fixed", "Прямые постоянные", "direct_fixed", "subtotal",
        140, 0, -1, "calendar", True, "active",
        {"op": "sum", "block": "direct_fixed"}, None, None, None,
    ),
    (
        "cook_payroll", "Зарплата поваров (оклады и удержания)", "direct_fixed", "source",
        150, 1, -1, "calendar", True, "active", None, None, None,
        "Начисления производственной ведомости минус штрафы, КРОМЕ штрафов по ревизии "
        "(они идут отдельной строкой «Результаты ревизии»)",
    ),
    (
        "pizza_box_inventory", "Результаты инвентаризации коробки д/пиццы", "direct_fixed",
        "source", 160, 1, -1, "calendar", True, "active", None, None, None,
        "iiko, инвентаризация упаковки: четыре товара коробок для пиццы, «разница сумма, р». "
        "Знак iiko сохраняется. Относится на направление «Пицца»",
    ),
    (
        "gross_profit_by_direction", "Валовая прибыль по направлениям", "direct_fixed",
        "subtotal", 170, 0, 1, "calendar", True, "active",
        {"op": "sub", "args": ["margin_income", "direct_fixed"]}, None, None, None,
    ),
    (
        "gross_profit_by_direction_ratio", "Рентабельность по направлениям, %", "direct_fixed",
        "ratio", 180, 1, 1, "calendar", False, "active",
        {"op": "ratio", "num": "gross_profit_by_direction", "den": "revenue"}, None, None, None,
    ),
    # --------------------------------------------------- Общепроизводственные
    (
        "overhead", "Общепроизводственные", "overhead", "subtotal",
        190, 0, -1, "calendar", False, "active",
        {"op": "sum", "block": "overhead"}, None, None, None,
    ),
    (
        "rent_gagarina", "Аренда торговой точки Гагарина", "overhead", "source",
        200, 1, -1, "document", False, "not_used", None, None, None,
        "Точка не работает с 2024 года",
    ),
    (
        "rent_chernikova", "Аренда торговой точки Черникова", "overhead", "source",
        210, 1, -1, "document", False, "active", None, None, None,
        "Договор аренды помещения, начисление последним днём месяца",
    ),
    (
        "administrator_payroll", "Зарплата администраторов", "overhead", "source",
        220, 1, -1, "calendar", False, "active", None, None, None,
        "Начисления ведомости по роли «администратор» минус штрафы, кроме ревизионных",
    ),
    (
        "bonuses", "Премии поваров, шеф-повара, администраторов", "overhead", "source",
        230, 1, -1, "calendar", False, "active", None, None, None,
        "Ручные корректировки ведомости типа «премия»",
    ),
    (
        "writeoffs", "Списание продукции и сырья", "overhead", "source",
        240, 1, -1, "calendar", False, "active", None, None, None,
        "iiko, акты списания со статусом PROCESSED, сумма items[].cost",
    ),
    (
        "audit_results", "Результаты ревизии", "overhead", "source",
        250, 1, -1, "calendar", False, "active", None, None, None,
        "Продуктовые инвентаризации iiko (знак инвертируется: недостача — положительный "
        "расход) МИНУС штрафы по ревизиям из ведомости. Товары упаковки и коробок "
        "исключаются — они идут своими строками",
    ),
    (
        "courier_service", "Траты на курьерскую службу", "overhead", "source",
        260, 1, -1, "calendar", False, "active", None, None, None,
        "iiko, отчёт о прибылях и убытках, строка «Зарплата курьеров». Оклад старшего "
        "курьера сюда НЕ входит — он в «Зарплате администрации»",
    ),
    (
        "acquiring", "Комиссия за эквайринг", "overhead", "source",
        270, 1, -1, "cash", False, "active", None, None, None,
        "Три компонента: комиссия по картам и по приёму платежей — из назначения зачислений "
        "Сбера (приходят нетто), плюс отдельные списания по терминалам",
    ),
    (
        "transport", "Транспортные услуги", "overhead", "source",
        280, 1, -1, "document", False, "active", None, None, None,
        "Документы перевозчиков; касса добирает то, что ещё не признано",
    ),
    (
        "staff_meals", "Расходы на питание персонала", "overhead", "source",
        290, 1, -1, "cash", False, "active", None, None, None, "ДДС, одноимённая статья",
    ),
    (
        "automation", "Оплата систем автоматизации", "overhead", "source",
        300, 1, -1, "document", False, "active", None, None, None,
        "Подписочные контракты по периоду услуги: iiko, Лемма, ДоксИнбокс, Реви (⅙ платежа)",
    ),
    (
        "shop_maintenance", "Содержание торговых точек", "overhead", "source",
        310, 1, -1, "cash", False, "active", None, None, None,
        "Два компонента: касса по статье плюс закупка расходников из приходных накладных iiko "
        "по whitelist. ФОТ уборщиц сюда не входит — он в «Зарплате администрации»",
    ),
    (
        "warehouse_rent", "Аренда склада", "overhead", "source",
        320, 1, -1, "document", False, "active", None, None, None, "Договор аренды склада",
    ),
    (
        "packaging_inventory",
        "Результаты инвентаризации упаковки (кроме коробок для пиццы)", "overhead", "source",
        330, 1, -1, "calendar", False, "active", None, None, None,
        "iiko, инвентаризация упаковки по whitelist. Знак сохраняется. Коробки для пиццы "
        "исключены — у них своя строка",
    ),
    (
        "aux_goods", "Вспомогательные товары(учет по факту оплаты)", "overhead", "source",
        340, 1, -1, "calendar", False, "active", None, None, None,
        "iiko, приходные накладные PROCESSED по whitelist вспомогательных товаров",
    ),
    (
        "payroll_taxes", "Налоги с ЗП", "overhead", "source",
        350, 1, -1, "cash", False, "active", None, None, None,
        "ДДС, статья «Налоги с з/п». Из ведомости эту строку брать нельзя: взносы "
        "работодателя в неё не входят",
    ),
    (
        "accumulation_fund", "Накопительный фонд", "overhead", "source",
        360, 1, -1, "calendar", False, "active", None, None, None,
        "Начисление фонда в ведомости",
    ),
    (
        "gross_profit", "Валовая прибыль", "gross", "subtotal",
        370, 0, 1, "calendar", False, "active",
        {"op": "sub", "args": ["gross_profit_by_direction", "overhead"]}, None, None, None,
    ),
    (
        "gross_profit_ratio", "Рентабельность по валовой прибыли, %", "gross", "ratio",
        380, 1, 1, "calendar", False, "active",
        {"op": "ratio", "num": "gross_profit", "den": "revenue"}, 0.120, 0.250, None,
    ),
    # ------------------------------------------------------------- Косвенные
    (
        "indirect_costs", "Косвенные расходы", "indirect", "subtotal",
        390, 0, -1, "calendar", False, "active",
        {"op": "sum", "args": ["admin_costs", "commercial_costs"]}, None, None, None,
    ),
    (
        "admin_costs", "Административные", "admin", "subtotal",
        400, 0, -1, "calendar", False, "active",
        {"op": "sum", "block": "admin"}, 0.080, 0.150, None,
    ),
    (
        "admin_payroll", "Зарплата администрации", "admin", "source",
        410, 1, -1, "calendar", False, "active", None, None, None,
        "Административная ведомость (полумесячные периоды). Сюда же оклад старшего курьера, "
        "уборщиц и посудомоек — они начисляются здесь, хотя платятся другими статьями",
    ),
    (
        "utilities_gagarina", "Коммунальные платежи Гагарина", "admin", "source",
        420, 1, -1, "document", False, "not_used", None, None, None,
        "Точка не работает с 2024 года",
    ),
    (
        "utilities_chernikova", "Коммунальные платежи Черникова", "admin", "source",
        430, 1, -1, "document", False, "active", None, None, None,
        "Счета арендодателя по воде, газу и электричеству; месяц — по периоду потребления. "
        "Счета приходят раздельно, поэтому месяц может ждать часть документов",
    ),
    (
        "rko", "РКО", "admin", "source",
        440, 1, -1, "cash", False, "active", None, None, None, "ДДС, одноимённая статья",
    ),
    (
        "bank_fees", "Прочие банковские комиссии", "admin", "source",
        450, 1, -1, "cash", False, "active", None, None, None, "ДДС, одноимённая статья",
    ),
    (
        "fd_nk_services", "Услуги ФД и НК", "admin", "source",
        460, 1, -1, "document", False, "active", None, None, None,
        "Договоры обслуживания финансового директора и налогового консультанта",
    ),
    (
        "staff_expenses", "Расходы на персонал", "admin", "source",
        470, 1, -1, "cash", False, "active", None, None, None, "ДДС, одноимённая статья",
    ),
    (
        "telecom", "Телекоммуникации", "admin", "source",
        480, 1, -1, "document", False, "active", None, None, None,
        "Mango Office (ручной ввод: личный кабинет закрыт капчей) плюс фиксированные "
        "iHC.ru и МИКРОЭЛ по документам",
    ),
    (
        "recruiting", "Поиск и найм персонала", "admin", "source",
        490, 1, -1, "cash", False, "active", None, None, None, "ДДС, одноимённая статья",
    ),
    (
        "commercial_costs", "Коммерческие", "commercial", "subtotal",
        500, 0, -1, "calendar", False, "active",
        {"op": "sum", "block": "commercial"}, 0.030, 0.100, None,
    ),
    (
        "aggregator_commission", "Комиссия агрегатору", "commercial", "source",
        510, 1, -1, "document", False, "active", None, None, None,
        "Агрегатор «К порогу» (ИП Вишневецкий), документы контрагента",
    ),
    (
        "targeted_ads", "Таргетированная реклама", "commercial", "source",
        520, 1, -1, "document", False, "active", None, None, None,
        "УПД подрядчика по обслуживанию и рекламному бюджету",
    ),
    (
        "outdoor_ads", "Наружная реклама", "commercial", "source",
        530, 1, -1, "document", False, "active", None, None, None,
        "Документы подрядчиков наружной рекламы (в каталоге ДДС статья называется "
        "«Баннерная реклама»)",
    ),
    (
        "context_ads", "Контекстная реклама", "commercial", "source",
        540, 1, -1, "document", False, "active", None, None, None,
        "Абонентское обслуживание по счёту плюс фактический бюджет кампаний",
    ),
    (
        "printed_materials", "Печатные материалы НЕ ИСПОЛЬЗУЕМ!", "commercial", "source",
        550, 1, -1, "cash", False, "not_used", None, None, None, "Строка не используется",
    ),
    (
        "flyers", "Листовки", "commercial", "source",
        560, 1, -1, "cash", False, "active", None, None, None,
        "ДДС, одноимённая статья: печать и разнос",
    ),
    (
        "seo", "SEO - оптимизация", "commercial", "source",
        570, 1, -1, "document", False, "active", None, None, None,
        "Счета подрядчика по SEO-продвижению, месяц — по периоду услуги",
    ),
    (
        "other_marketing", "Прочие маркетинговые услуги", "commercial", "source",
        580, 1, -1, "cash", False, "active", None, None, None,
        "ДДС, одноимённая статья: фотограф, дизайнер, полиграфия",
    ),
    (
        "website_app", "Сайт и приложение", "commercial", "source",
        590, 1, -1, "document", False, "active", None, None, None,
        "УПД StarterApp за месяц пользования платформой",
    ),
    (
        "ebitda", "Операционная прибыль (EBITDA)", "ebitda", "subtotal",
        600, 0, 1, "calendar", False, "active",
        {"op": "sub", "args": ["gross_profit", "indirect_costs"]}, None, None, None,
    ),
    (
        "ebitda_ratio", "Рентабельность по операционной прибыли, %", "ebitda", "ratio",
        610, 1, 1, "calendar", False, "active",
        {"op": "ratio", "num": "ebitda", "den": "revenue"}, 0.030, 0.070, None,
    ),
    # ---------------------------------------------------------- Ниже EBITDA
    (
        "below_income", "Доходы ниже EBITDA", "below_income", "subtotal",
        620, 0, 1, "calendar", False, "active",
        {"op": "sum", "block": "below_income"}, None, None, None,
    ),
    (
        "other_income", "Прочие доходы", "below_income", "source",
        630, 1, 1, "cash", False, "active", None, None, None, "ДДС, прочие поступления",
    ),
    (
        "unclaimed_deposits_writeoff", "Списание невостребованных депозитов", "below_income",
        "source", 640, 1, 1, "calendar", False, "active", None, None, None,
        "Списание депозитов сотрудников в ведомости",
    ),
    (
        "below_expense", "Расходы ниже EBITDA", "below_expense", "subtotal",
        650, 0, -1, "calendar", False, "active",
        {"op": "sum", "block": "below_expense"}, None, None, None,
    ),
    (
        "other_expenses", "Прочие расходы", "below_expense", "source",
        660, 1, -1, "cash", False, "active", None, None, None, "ДДС, одноимённая статья",
    ),
    (
        "loan_interest", "Проценты по кредитам", "below_expense", "source",
        670, 1, -1, "cash", False, "active", None, None, None,
        "Только проценты, без тела кредита",
    ),
    (
        "taxes", "Налоги", "below_expense", "source",
        680, 1, -1, "calendar", False, "active", None, None, None,
        "Начисления модуля «Налоги» (решение владельца 03.08.2026: начисление, а не "
        "расчётная модель 7% из методологии)",
    ),
    (
        "overdraft_fee", "Комиссия за овердрафт", "below_expense", "source",
        690, 1, -1, "cash", False, "active", None, None, None,
        "ДДС, статья «Овердрафт»: перечисление процентов",
    ),
    (
        "depreciation", "Амортизация", "below_expense", "source",
        700, 1, -1, "calendar", False, "active", None, None, None,
        "Модуль «Учёт ОС», начисленная амортизация месяца",
    ),
    (
        "fines_penalties", "Штрафы и пени", "below_expense", "source",
        710, 1, -1, "cash", False, "active", None, None, None,
        "ДДС. Налоговые пени сюда не попадают — они внутри строки «Налоги»",
    ),
    (
        "net_profit", "Чистая прибыль", "net", "subtotal",
        720, 0, 1, "calendar", False, "active",
        {
            "op": "sub",
            "args": [{"op": "sum", "args": ["ebitda", "below_income"]}, "below_expense"],
        },
        None, None, "EBITDA плюс доходы ниже EBITDA минус расходы ниже EBITDA",
    ),
    (
        "net_profit_ratio", "Рентабельность по чистой прибыли, %", "net", "ratio",
        730, 1, 1, "calendar", False, "active",
        {"op": "ratio", "num": "net_profit", "den": "revenue"}, None, None, None,
    ),
    # ------------------------------------------------------------- Служебные
    (
        "__out_of_pnl__", "Вне ОПиУ", "service", "service",
        900, 0, 1, "cash", False, "active", None, None, None,
        "Переводы между кошельками, тело займов, депозиты, дивиденды, а также то, чей факт "
        "принадлежит другому слою (товар приходит фудкостом, зарплата — из ведомости)",
    ),
    (
        "__unmapped__", "Не разнесено", "service", "service",
        910, 0, 1, "cash", False, "active", None, None, None,
        "Проводка без статьи или без правила. Обязана быть нулём: это и есть защита от "
        "тихой потери",
    ),
    (
        "__accrual_settled__", "Исключено как расчёт по признанию", "service", "service",
        920, 0, 1, "cash", False, "active", None, None, None,
        "Оплата, закрывающая признанный расход: сам расход уже взят начислением",
    ),
]


def upgrade() -> None:
    op.create_table(
        "pnl_line",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("block", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("formula", postgresql.JSONB, nullable=True),
        sa.Column("sort_order", sa.Integer, nullable=False),
        sa.Column("level", sa.SmallInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("sign_role", sa.SmallInteger, nullable=False),
        sa.Column("month_basis", sa.String(16), nullable=False),
        sa.Column(
            "direction_aware", sa.Boolean, nullable=False, server_default=sa.text("false")
        ),
        sa.Column("norm_min", sa.Numeric(6, 3), nullable=True),
        sa.Column("norm_max", sa.Numeric(6, 3), nullable=True),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default=sa.text("'active'")
        ),
        sa.Column("source_note", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "kind in ('source', 'memo', 'subtotal', 'ratio', 'stat', 'service')",
            name="ck_pnl_line_kind",
        ),
        sa.CheckConstraint(
            "status in ('active', 'not_used', 'historic')", name="ck_pnl_line_status"
        ),
        sa.CheckConstraint(
            "month_basis in ('document', 'cash', 'calendar')", name="ck_pnl_line_month_basis"
        ),
        sa.CheckConstraint("sign_role in (-1, 1)", name="ck_pnl_line_sign_role"),
    )
    op.create_index("ix_pnl_line_sort_order", "pnl_line", ["sort_order"])

    # Сид идемпотентен: повторный прогон обновляет справочные поля, но не трогает id —
    # на code завязаны правила источников и ручные числа.
    connection = op.get_bind()
    for row in LINES:
        (
            code, title, block, kind, sort_order, level, sign_role, month_basis,
            direction_aware, status, formula, norm_min, norm_max, source_note,
        ) = row
        connection.execute(
            sa.text(
                """
                INSERT INTO pnl_line (
                    id, code, title, block, kind, formula, sort_order, level, sign_role,
                    month_basis, direction_aware, norm_min, norm_max, status, source_note
                ) VALUES (
                    gen_random_uuid(), :code, :title, :block, :kind,
                    CAST(:formula AS jsonb), :sort_order, :level, :sign_role,
                    :month_basis, :direction_aware, :norm_min, :norm_max, :status, :source_note
                )
                ON CONFLICT (code) DO UPDATE SET
                    title = EXCLUDED.title,
                    block = EXCLUDED.block,
                    kind = EXCLUDED.kind,
                    formula = EXCLUDED.formula,
                    sort_order = EXCLUDED.sort_order,
                    level = EXCLUDED.level,
                    sign_role = EXCLUDED.sign_role,
                    month_basis = EXCLUDED.month_basis,
                    direction_aware = EXCLUDED.direction_aware,
                    norm_min = EXCLUDED.norm_min,
                    norm_max = EXCLUDED.norm_max,
                    status = EXCLUDED.status,
                    source_note = EXCLUDED.source_note,
                    updated_at = now()
                """
            ),
            {
                "code": code,
                "title": title,
                "block": block,
                "kind": kind,
                "formula": None if formula is None else __import__("json").dumps(formula),
                "sort_order": sort_order,
                "level": level,
                "sign_role": sign_role,
                "month_basis": month_basis,
                "direction_aware": direction_aware,
                "norm_min": norm_min,
                "norm_max": norm_max,
                "status": status,
                "source_note": source_note,
            },
        )


def downgrade() -> None:
    op.drop_index("ix_pnl_line_sort_order", table_name="pnl_line")
    op.drop_table("pnl_line")

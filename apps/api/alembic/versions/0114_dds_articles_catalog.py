"""dds articles catalog from methodology (59 articles)

Loads the canonical ДДС article catalog from ``business-docs/finance/dds-methodology.md``
(лист ``ДДС статьи``): 44 operating + 7 financing + 6 investing + 2 technical = 59 articles.

Strategy (variant A, agreed with owner):
- Articles already present by exact name (``Оплата поставщикам``, ``Налоги``, ``РКО``)
  are updated in place (id kept → 52 existing transactions keep their FK).
- Missing canonical articles are inserted with a transliterated code.
- Leftover placeholder rows from the initial dev seed are deactivated (is_active=false),
  NOT deleted, so cashflow_transactions FKs stay intact.
- Engine-critical rows (revenue_acquiring_tbank — live rule target, internal_transfer —
  transfer matching, unknown — fallback) stay active under the hood; they will be merged
  into the canonical set during the rules redesign stage.

Block labels are loaded as-is from the template; mislabels (loans/interest under
investing) are intentionally NOT fixed here — that is the next stage.

Revision ID: 0114_dds_articles_catalog
Revises: 0113_inventory_group_rates
Create Date: 2026-06-16
"""

from __future__ import annotations

import re
import uuid

from alembic import op
from sqlalchemy import text

revision = "0114_dds_articles_catalog"
down_revision = "0113_inventory_group_rates"
branch_labels = None
depends_on = None


# (name, movement_type, activity_type, description)
CATALOG: tuple[tuple[str, str, str, str], ...] = (
    ("Поступление — Перевод между счетами", "inflow", "technical", "Поступление денег с нашего счета или другого кошелька"),
    ("Выбытие — Перевод между счетами", "outflow", "technical", "Перевод денег на другой наш счет или кошелёк"),
    ("Возврат переплаты от поставщиков", "inflow", "operating", "Возврат переплаты от поставщиков"),
    ("Резервы поступление", "inflow", "operating", "Поступление денег с резервных фондов"),
    ("Резервы вывод", "outflow", "operating", "Вывод денег в резервные фонды"),
    ("Корретировки кассы +", "inflow", "operating", "Корректировка расхождений кассы факт с айко"),
    ("Корретировки кассы -", "outflow", "operating", "Корректировка расхождений кассы факт с айко"),
    ("Поступление денег с торг. точек", "inflow", "operating", "Поступления от продаж, на торговых точках, по кассе и безналу"),
    ("Прочие поступления", "inflow", "operating", "Поступления не описанные в других статьях"),
    ("Возвраты клиентам", "outflow", "operating", "Мы вернули деньги клиенту, за ранее купленный товар"),
    ("Оплата поставщикам", "outflow", "operating", "Закупка товара для последующей перепродажи на торговых точках, сырья для производства"),
    ("Прочие расходы", "outflow", "operating", "Прочие выбытия, не упомянутые в других статьях! Оплата услуг ОФД"),
    ("Транспортные услуги", "outflow", "operating", "Оплаты транспортным компаниям и курьерским службам, за перевозку товаров до точек, и документов поставщикам"),
    ("Курьерская служба -", "outflow", "operating", "Оплата курьерам за доставку товара, начальнику службы доставки"),
    ("Курьерская служба +", "inflow", "operating", "Перемещение части ЗП в гарантийный депозит"),
    ("Эквайринг", "outflow", "operating", "Комиссии за эквайринг взимаемые, при оплате по безналу"),
    ("РКО", "outflow", "operating", "Абонентская плата за обслуживание РС, и комиссии за платёжные поручения"),
    ("Овердрафт", "outflow", "operating", "Перечисление %% по овердрафту"),
    ("Налоги с з/п", "outflow", "operating", "НДФЛ+ПФР"),
    ("Налоги", "outflow", "operating", "Патентная система НО, пенсионные взносы ИП"),
    ("Зарплата производственного персонала", "outflow", "operating", "Оплата, зарплаты поваров, администраторов"),
    ("Зарплата административного персонала", "outflow", "operating", "Зарплата менеджера, директора по развитию, технического директора, щеф-повара"),
    ("Баннерная реклама", "outflow", "operating", "Оплата баннеров наружной рекламы"),
    ("Таргетированная реклама", "outflow", "operating", "Оплаты услуг таргетолога, включая абонентскую плату за управление и рекламный бюджет"),
    ("Контекстная реклама", "outflow", "operating", "Оплаты услуг контекстолога, включая абонентскую плату за управление и рекламный бюджет"),
    ("Печатные материалы", "outflow", "operating", "Оплата наклеек на пакеты, брошюр и прочих печатных материалов"),
    ("Листовки", "outflow", "operating", "Оплата разноса и печати листовок"),
    ("Комиссия партнерам", "outflow", "operating", "Оплата Комиссии партнерам за заказы"),
    ("SEO-оптимизация", "outflow", "operating", "Оплата услуг специалистов по SEO-оптимизации сайта"),
    ("Реклама на Яндекс-картах", "outflow", "operating", "Оплата рекламы на яндекс картах, включая настройку и рекламный бюджет"),
    ("Комиссия агрегатору", "outflow", "operating", "Оплата комиссии агрегатору К порогу"),
    ("Услуги ФД и НК", "outflow", "operating", "Зарплата финансового директора и налогового консультанта"),
    ("Прочие маркетинговые услуги", "outflow", "operating", "Услуги фотографа и дизайнера"),
    ("Обучение персонала", "outflow", "operating", "Компенсация курсов, для сотрудников офиса, обучение бариста для работы на точках"),
    ("Расходы на питание персонала", "outflow", "operating", "Покупка продуктов для персонального питания"),
    ("Расходы на персонал", "outflow", "operating", "Расходы на организацию корпоративов, подарки сотрудникам и т.д., оплата бесплатных переводов Сбербанк для выдачи зарплат"),
    ("Сайт и приложение", "outflow", "operating", "Оплата услуг сайта и приложения"),
    ("Оплаты систем автоматизации", "outflow", "operating", 'Оплата ресторанного комплекса iiko, сервиса ЭДО Докс ин бокс, Оплата Леммы (Тех. поддержка iiko), Оплата системы "Ревви"'),
    ("Телекоммуникации", "outflow", "operating", "Оплата интернета, сотовой связи, телефонии"),
    ("Поиск и найм персонала", "outflow", "operating", "Оплата работы стороннего HR, рекрутера, оплата площадок hh.ru, superjob.ru, rabota.ru, Avito"),
    ("Содержание торговых точек", "outflow", "operating", "Закупка расходников на торговые точки — фильтры для воды, спецодежда, лампочки, и прочие мелкие расходы по содержанию точки, покупка оборудования дешевле 10 т.р., оплата труда приходящей уборщицы, ремонт торговых точек"),
    ("Содержание офиса", "outflow", "operating", "Покупка канцелярии в офис, мелкой оргтехники не дороже 10 т.р., покупка воды, покупка хозтоваров, оплата труда приходящей уборщицы."),
    ("Аренда торговых точек", "outflow", "operating", "Платежи за аренду и коммунальные услуги, вывоз мусора с торговых точек, оплата охраны"),
    ("Аренда склада", "outflow", "operating", "Аренда складских помещений"),
    ("Аренда офиса", "outflow", "operating", "Платежи за аренду и КУ офиса"),
    ("Покупка наличности", "outflow", "operating", "Комиссии за получение наличных денег"),
    ("Получение кредитов и займов", "inflow", "financing", "Нам дали в долг"),
    ("Оплаты по кредитам и займам", "outflow", "financing", "Мы оплатили наши кредиты и займы"),
    ("Получение овердрафта", "inflow", "financing", "Получили овердрафт"),
    ("Погашение овердрафта", "outflow", "financing", "Погасили овердрафт"),
    ("Поступление денег от собственников", "inflow", "financing", "Собственник внёс деньги в кассу/на р/с"),
    ("Возврат денег собственникам", "outflow", "financing", "Собственник получил деньги из кассы/р/с"),
    ("Дивиденды", "outflow", "financing", "Выплата дивидендов собственникам"),
    ("Продажа ОС", "inflow", "investing", "Продажа мебели, техники"),
    ("Покупка ОС", "outflow", "investing", "Покупка мебели, техники (дороже 10 т.р.)"),
    ("Ремонт ОС", "outflow", "investing", "Капитальный ремонт техники — приводит к увеличению текущей стоимости, и продлевает срок службы"),
    ("Выдача кредитов и займов", "outflow", "investing", "Мы выдали кредит или займ"),
    ("Возврат кредитов и займов", "inflow", "investing", "Нам вернули кредит или займ"),
    ("Прочие поступл. от фин. операций", "inflow", "investing", "Проценты на остаток по РС"),
)

# Existing rows kept active even though they are not in the canonical catalog:
# the bank classifier assigns by article_id and never filters on is_active, but
# these are still meaningful targets that the rules redesign will consolidate.
KEEP_ACTIVE_CODES = ("revenue_acquiring_tbank", "internal_transfer", "unknown")

# Placeholders deactivated by the upgrade (kept for downgrade reactivation).
PLACEHOLDER_CODES = (
    "revenue_cash",
    "revenue_acquiring_sber",
    "employee_advance",
    "rent",
    "customer_refund",
    "payroll_payout",
    "supplier_staff_payment",
    "overdraft_fee",
    "loan_principal_payment",
)

# Canonical names that already existed before this migration (updated in place).
PRESERVED_NAMES = ("Оплата поставщикам", "Налоги", "РКО")

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _slugify(name: str) -> str:
    base = "".join(_TRANSLIT.get(ch, ch) for ch in name.lower())
    base = re.sub(r"[^a-z0-9]+", "_", base).strip("_")[:48]
    return base or "article"


def upgrade() -> None:
    conn = op.get_bind()
    existing = conn.execute(text("SELECT id, code, name FROM dds_articles")).all()
    existing_by_name = {row.name: row for row in existing}
    used_codes = {row.code for row in existing}
    canonical_names = {item[0] for item in CATALOG}

    for name, movement, activity, description in CATALOG:
        current = existing_by_name.get(name)
        if current is not None:
            conn.execute(
                text(
                    "UPDATE dds_articles SET movement_type=:movement, activity_type=:activity,"
                    " description=:description, is_active=true, updated_at=now() WHERE id=:id"
                ),
                {
                    "movement": movement,
                    "activity": activity,
                    "description": description,
                    "id": current.id,
                },
            )
            continue

        code = _slugify(name)
        candidate = code
        suffix = 2
        while candidate in used_codes:
            candidate = f"{code}_{suffix}"
            suffix += 1
        used_codes.add(candidate)
        conn.execute(
            text(
                "INSERT INTO dds_articles (id, code, name, movement_type, activity_type,"
                " parent_id, is_active, description)"
                " VALUES (:id, :code, :name, :movement, :activity, NULL, true, :description)"
            ),
            {
                "id": str(uuid.uuid4()),
                "code": candidate,
                "name": name,
                "movement": movement,
                "activity": activity,
                "description": description,
            },
        )

    # Deactivate leftover placeholders (snapshot taken before inserts above).
    for row in existing:
        if row.name in canonical_names:
            continue
        if row.code in KEEP_ACTIVE_CODES:
            continue
        conn.execute(
            text("UPDATE dds_articles SET is_active=false, updated_at=now() WHERE id=:id"),
            {"id": row.id},
        )


def downgrade() -> None:
    conn = op.get_bind()

    # Remove inserted canonical articles that are not referenced by any transaction.
    for name, _movement, _activity, _description in CATALOG:
        if name in PRESERVED_NAMES:
            continue
        row = conn.execute(
            text("SELECT id FROM dds_articles WHERE name=:name"), {"name": name}
        ).first()
        if row is None:
            continue
        refs = conn.execute(
            text("SELECT count(*) FROM cashflow_transactions WHERE article_id=:id"),
            {"id": row.id},
        ).scalar()
        if refs:
            continue
        conn.execute(text("DELETE FROM dds_article_aliases WHERE article_id=:id"), {"id": row.id})
        conn.execute(text("DELETE FROM dds_articles WHERE id=:id"), {"id": row.id})

    # Reactivate placeholders deactivated by the upgrade.
    for code in PLACEHOLDER_CODES:
        conn.execute(
            text("UPDATE dds_articles SET is_active=true, updated_at=now() WHERE code=:code"),
            {"code": code},
        )

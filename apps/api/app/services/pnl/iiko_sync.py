"""Синхронизация месячных фактов iiko в зеркало ОПиУ.

ЧТО ТЯНЕМ. Выручку без скидок и со скидками, себестоимость продаж (фудкост) — по направлениям,
комиссию партнёров из «Отчёта о партнёрах» и зарплату курьеров. Всё остальное в отчёте живёт
внутри приложения.

ТОЛЬКО СОХРАНЁННЫЕ ПРЕСЕТЫ, НИКАКОГО СВОЕГО ТЕЛА ЗАПРОСА. Собрать OLAP-запрос руками легко, и
он даже вернёт похожие числа — но БЕЗ фильтров удалённых заказов и списанных позиций, потому
что фильтры живут внутри пресета. Разница не теоретическая: на январе 2026 сырой запрос дал
4 934 984 ₽ против 4 689 053 ₽ по пресету. Завышенная выручка тянет за собой завышенный налог,
поэтому источник — только пресет.

ВЕРХНЯЯ ГРАНИЦА ДАТЫ У ПРЕСЕТОВ ИСКЛЮЧАЮЩАЯ. Для полного месяца передаём первое число
следующего. Проверено на апреле 2026: ``dateTo=2026-04-30`` дал 238 043 ₽ зарплаты курьеров,
``dateTo=2026-05-01`` — 248 401 ₽. Разница ровно в последнем дне месяца, и ошибка выглядит
правдоподобно — поэтому границы считает один хелпер, а не каждый вызов сам.

iiko Server ПРИВЯЗАН К IP ПРОДА. На локали и превью синк не работает, зеркало остаётся пустым,
и отчёт честно показывает «нет данных». Это не сбой конфигурации, а нормальное состояние
среды; бэкфилл делается на проде однократно.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import anyio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.pnl import (
    PnlIikoFact,
    PnlIikoGoodsFact,
    PnlIikoProductObservation,
    PnlIikoStockFact,
    PnlPartnerCommissionFact,
    PnlPartnerCommissionRule,
    PnlProductMonthlyDecision,
    PnlProductWhitelist,
)
from app.services.iiko_sync import (
    _load_export_employees_module,
    _load_source_credential_env,
    _request_iiko_with_incomplete_read_retry,
)

#: Сохранённый пресет «Отчет о выручки по направлениям» (тип SALES). Группировка по
#: подразделению и месту приготовления, фильтры удалённых заказов — внутри пресета.
REVENUE_PRESET_ID = "73a25778-dafb-4065-a820-1e9c7da6fed6"
#: Сохранённый пресет «P&L по складам» (тип TRANSACTIONS) — из него берётся строка
#: «Зарплата курьеров».
PNL_PRESET_ID = "8c13763a-35bf-9f27-017f-5468b1e70021"
PARTNER_PRESET_ID = "28b87ee7-0af5-41ad-baae-de735189169c"

COURIER_ACCOUNT_NAME = "Зарплата курьеров"
COURIER_ACCOUNT_TYPE = "EXPENSES"

#: Управленческое направление ОПиУ ← место приготовления в iiko. «Роллы» собираются из ДВУХ
#: источников: суши и специи — так устроен учёт на точке, и в эталоне это одна колонка.
DIRECTION_BY_COOKING_PLACE = {
    "бар": "bar",
    "пицц": "pizza",
    "шаур": "hot_shop",
    "суш": "rolls",
    "спец": "rolls",
}

METRIC_REVENUE_GROSS = "revenue_gross"
METRIC_REVENUE_NET = "revenue_net"
METRIC_FOOD_COST = "food_cost"
METRIC_COURIER_SALARY = "courier_salary"
METRIC_PARTNER_COMMISSION = "partner_commission"

_PARTNER_SPACES = re.compile(r"\s+")


def normalize_partner_name(value: Any) -> str:
    """Стабильный ключ имени из OLAP: регистр и случайные пробелы не меняют партнёра."""
    return _PARTNER_SPACES.sub(" ", str(value or "").strip()).casefold()


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def month_bounds_exclusive(month: dt.date) -> tuple[dt.date, dt.date]:
    """Границы месяца для пресетов: начало включительно, конец — первое число следующего."""
    first = month.replace(day=1)
    following = (first + dt.timedelta(days=32)).replace(day=1)
    return first, following


def map_direction(cooking_place: str | None) -> str:
    """Место приготовления → направление ОПиУ. Незнакомое НЕ сваливаем в «итого» молча."""
    text = (cooking_place or "").strip().casefold()
    if not text:
        return "unmapped"
    for marker, direction in DIRECTION_BY_COOKING_PLACE.items():
        if marker in text:
            return direction
    return "unmapped"


def _number(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0.00")
    try:
        return Decimal(str(value).replace(",", ".").replace("\xa0", "").replace(" ", ""))
    except Exception:  # noqa: BLE001 — любое нечисло трактуем как ноль, но не падаем
        return Decimal("0.00")


def _field(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        for key in (name, f"{name}.sum"):
            if key in row and row[key] not in (None, ""):
                return row[key]
    return None


@dataclass(slots=True)
class PartnerCommissionAmount:
    """Одна строка будущего леджера «Расчёты с партнёрами»."""

    rule_id: Any
    partner_key: str
    partner_name: str
    revenue_amount: Decimal
    commission_rate: Decimal
    commission_amount: Decimal
    rows_count: int
    source_ref: str


@dataclass(slots=True)
class SyncResult:
    """Что синк сделал за месяц — для лога и для предупреждений в отчёте."""

    month: dt.date
    facts: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    unmapped_directions: set[str] = field(default_factory=set)
    rows_seen: int = 0
    changed: list[str] = field(default_factory=list)
    fact_rows_count: dict[tuple[str, str], int] = field(default_factory=dict)
    fact_source_ref: dict[tuple[str, str], str] = field(default_factory=dict)
    partner_facts: list[PartnerCommissionAmount] = field(default_factory=list)


def aggregate_partner_rows(
    rows: list[dict[str, Any]],
    rules: dict[str, tuple[Any, str, Decimal, str]],
) -> tuple[list[PartnerCommissionAmount], set[str]]:
    """Выручка со скидкой по партнёру → договорная комиссия.

    ``rules`` содержит: нормализованное имя → (id, отображаемое имя, ставка, preset id).
    Телефон клиента из группировки OLAP намеренно не сохраняется — для расчёта он не нужен.
    """
    revenue: dict[str, Decimal] = {}
    counts: dict[str, int] = {}
    unmapped: set[str] = set()
    for row in rows:
        raw_name = str(_field(row, "Delivery.CustomerName") or "").strip()
        partner_key = normalize_partner_name(raw_name)
        if not partner_key:
            continue
        if partner_key not in rules:
            unmapped.add(raw_name or partner_key)
            continue
        revenue[partner_key] = revenue.get(partner_key, Decimal("0.00")) + _number(
            _field(row, "DishDiscountSumInt", "sumAfterDiscountWithoutVAT")
        )
        counts[partner_key] = counts.get(partner_key, 0) + 1

    result: list[PartnerCommissionAmount] = []
    for partner_key, (rule_id, partner_name, rate, preset_id) in rules.items():
        amount = money(revenue.get(partner_key, Decimal("0.00")))
        result.append(
            PartnerCommissionAmount(
                rule_id=rule_id,
                partner_key=partner_key,
                partner_name=partner_name,
                revenue_amount=amount,
                commission_rate=rate,
                commission_amount=money(amount * rate),
                rows_count=counts.get(partner_key, 0),
                source_ref=preset_id,
            )
        )
    return result, unmapped


def aggregate_revenue_rows(rows: list[dict[str, Any]]) -> SyncResult:
    """Свернуть строки пресета выручки в метрики по направлениям и «итого»."""
    result = SyncResult(month=dt.date.today())
    for row in rows:
        result.rows_seen += 1
        direction = map_direction(_field(row, "CookingPlace"))
        if direction == "unmapped":
            place = str(_field(row, "CookingPlace") or "(пусто)")
            result.unmapped_directions.add(place)
            continue
        gross = _number(_field(row, "DishSumInt"))
        net = _number(_field(row, "DishDiscountSumInt", "sumAfterDiscountWithoutVAT"))
        cost = _number(_field(row, "ProductCostBase.ProductCost"))
        for metric, value in (
            (METRIC_REVENUE_GROSS, gross),
            (METRIC_REVENUE_NET, net),
            (METRIC_FOOD_COST, cost),
        ):
            key = (metric, direction)
            result.facts[key] = result.facts.get(key, Decimal("0.00")) + value
            total_key = (metric, "total")
            result.facts[total_key] = result.facts.get(total_key, Decimal("0.00")) + value
    return result


def courier_salary_from_rows(rows: list[dict[str, Any]]) -> Decimal:
    """Строка «Зарплата курьеров» из пресета P&L.

    Оклад старшего курьера сюда НЕ входит — владелец подтвердил 03.08.2026, что он проходит
    административной ведомостью. Поэтому строка ОПиУ берётся отсюда целиком, без вычитаний.
    """
    total = Decimal("0.00")
    for row in rows:
        name = str(_field(row, "Account.Name") or "").strip()
        account_type = str(_field(row, "Account.Type") or "").strip()
        if name == COURIER_ACCOUNT_NAME and account_type == COURIER_ACCOUNT_TYPE:
            total += _number(_field(row, "Sum.ResignedSum"))
    return total


async def _fetch_preset(preset_id: str, start: dt.date, end_exclusive: dt.date) -> list[dict]:
    """Забрать строки сохранённого пресета за период. Синхронный клиент — в отдельном треде."""

    def _call() -> list[dict[str, Any]]:
        export_employees = _load_export_employees_module()
        export_employees.load_local_env()
        client = export_employees.IikoClient()
        _status, data = _request_iiko_with_incomplete_read_retry(
            client,
            f"/v2/reports/olap/byPresetId/{preset_id}",
            params={
                "summary": "false",
                "dateFrom": start.isoformat(),
                "dateTo": end_exclusive.isoformat(),
            },
        )
        return _parse_rows(data)

    return await anyio.to_thread.run_sync(_call)


def _parse_rows(data: bytes | str | list | dict) -> list[dict[str, Any]]:
    import json

    payload = data
    if isinstance(payload, (bytes, str)):
        payload = json.loads(payload)
    if isinstance(payload, dict):
        for key in ("data", "rows", "result"):
            if isinstance(payload.get(key), list):
                return [row for row in payload[key] if isinstance(row, dict)]
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


async def fetch_partner_commissions(
    session: AsyncSession,
    start: dt.date,
    end_exclusive: dt.date,
) -> tuple[list[PartnerCommissionAmount], set[str]]:
    """Прочитать каждый настроенный сохранённый пресет один раз и применить ставки."""
    rules = list(
        (
            await session.scalars(
                select(PnlPartnerCommissionRule).where(PnlPartnerCommissionRule.is_active.is_(True))
            )
        ).all()
    )
    by_preset: dict[str, list[PnlPartnerCommissionRule]] = {}
    for rule in rules:
        by_preset.setdefault(rule.source_preset_id, []).append(rule)

    facts: list[PartnerCommissionAmount] = []
    unmapped: set[str] = set()
    for preset_id, preset_rules in by_preset.items():
        rows = await _fetch_preset(preset_id, start, end_exclusive)
        mapping = {
            normalize_partner_name(rule.partner_key): (
                rule.id,
                rule.partner_name,
                Decimal(rule.commission_rate),
                rule.source_preset_id,
            )
            for rule in preset_rules
        }
        calculated, unknown = aggregate_partner_rows(rows, mapping)
        facts.extend(calculated)
        unmapped.update(unknown)
    return facts, unmapped


async def sync_month(session: AsyncSession, month: dt.date) -> SyncResult:
    """Залить месячные факты iiko в зеркало. Идемпотентно: повторный прогон обновляет."""
    await _load_source_credential_env(session)
    start, end_exclusive = month_bounds_exclusive(month)

    revenue_rows = await _fetch_preset(REVENUE_PRESET_ID, start, end_exclusive)
    result = aggregate_revenue_rows(revenue_rows)
    result.month = start

    courier_rows = await _fetch_preset(PNL_PRESET_ID, start, end_exclusive)
    result.facts[(METRIC_COURIER_SALARY, "total")] = courier_salary_from_rows(courier_rows)

    partner_facts, unknown_partners = await fetch_partner_commissions(session, start, end_exclusive)
    result.partner_facts = partner_facts
    result.facts[(METRIC_PARTNER_COMMISSION, "total")] = sum(
        (row.commission_amount for row in partner_facts), Decimal("0.00")
    )
    result.fact_rows_count[(METRIC_PARTNER_COMMISSION, "total")] = sum(
        row.rows_count for row in partner_facts
    )
    result.fact_source_ref[(METRIC_PARTNER_COMMISSION, "total")] = ",".join(
        sorted({row.source_ref for row in partner_facts})
    )
    if unknown_partners:
        result.changed.append("Партнёры без ставки: " + ", ".join(sorted(unknown_partners)))

    goods = await fetch_goods_metrics(session, start)
    for metric, amount in goods.metrics.items():
        result.facts[(metric, "total")] = amount
    # Провенанс товарных метрик: без него в зеркале встанет пресет выручки, и через месяц
    # никто не поймёт, откуда взялась цифра. Одна карта на все товарные эндпоинты.
    for metric, endpoint in GOODS_METRIC_SOURCE.items():
        if metric in goods.metrics:
            result.fact_source_ref[(metric, "total")] = endpoint
    result.changed.extend(goods.notes)

    for (metric, direction), amount in sorted(result.facts.items()):
        fact_key = (metric, direction)
        rows_count = result.fact_rows_count.get(fact_key, result.rows_seen)
        source_ref = result.fact_source_ref.get(fact_key) or (
            PNL_PRESET_ID if metric == METRIC_COURIER_SALARY else REVENUE_PRESET_ID
        )
        existing = await session.scalar(
            select(PnlIikoFact).where(
                PnlIikoFact.period_month == start,
                PnlIikoFact.metric_code == metric,
                PnlIikoFact.direction == direction,
            )
        )
        if existing is None:
            session.add(
                PnlIikoFact(
                    period_month=start,
                    metric_code=metric,
                    direction=direction,
                    amount=amount,
                    rows_count=rows_count,
                    source_ref=source_ref,
                )
            )
            continue
        if existing.amount != amount:
            # Цифра закрытого месяца изменилась. Молча переписать значило бы стереть след:
            # прошлое значение сохраняем и сообщаем наружу.
            result.changed.append(f"{metric}/{direction}: было {existing.amount}, стало {amount}")
            existing.previous_amount = existing.amount
        existing.amount = amount
        existing.rows_count = rows_count
        existing.source_ref = source_ref

    await session.execute(
        delete(PnlPartnerCommissionFact).where(PnlPartnerCommissionFact.period_month == start)
    )
    for item in result.partner_facts:
        session.add(
            PnlPartnerCommissionFact(
                period_month=start,
                rule_id=item.rule_id,
                partner_key=item.partner_key,
                partner_name=item.partner_name,
                revenue_amount=item.revenue_amount,
                commission_rate=item.commission_rate,
                commission_amount=item.commission_amount,
                rows_count=item.rows_count,
                source_ref=item.source_ref,
            )
        )

    # Детализация заменяется только по тем внешним источникам, которые успешно ответили.
    # Если iiko временно недоступен, старый проверенный разрез не стирается пустотой.
    if goods.refreshed_sources:
        if "inventory" in goods.refreshed_sources:
            await session.execute(
                delete(PnlIikoStockFact).where(PnlIikoStockFact.period_month == start)
            )
            for stock_fact in goods.stock_facts:
                session.add(
                    PnlIikoStockFact(
                        period_month=start,
                        iiko_product_guid=stock_fact.iiko_product_guid,
                        product_name=stock_fact.product_name,
                        product_code=stock_fact.product_code,
                        opening_quantity=stock_fact.opening_quantity,
                        opening_amount=stock_fact.opening_amount,
                        receipts_amount=stock_fact.receipts_amount,
                        closing_quantity=stock_fact.closing_quantity,
                        closing_amount=stock_fact.closing_amount,
                        consumption_amount=stock_fact.consumption_amount,
                        stores_count=stock_fact.stores_count,
                    )
                )
        await session.execute(
            delete(PnlIikoProductObservation).where(
                PnlIikoProductObservation.period_month == start,
                PnlIikoProductObservation.source_kind.in_(goods.refreshed_sources),
            )
        )
        for observation in goods.observations:
            session.add(
                PnlIikoProductObservation(
                    period_month=start,
                    source_kind=observation.source_kind,
                    iiko_product_guid=observation.iiko_product_guid,
                    product_name=observation.product_name,
                    product_code=observation.product_code,
                    amount=observation.amount,
                    rows_count=observation.rows_count,
                )
            )
        await session.execute(
            delete(PnlIikoGoodsFact).where(
                PnlIikoGoodsFact.period_month == start,
                PnlIikoGoodsFact.source_kind.in_(goods.refreshed_sources),
            )
        )
        product_guids = {detail.iiko_product_guid for detail in goods.details}
        product_names = {
            guid: name
            for guid, name in (
                await session.execute(
                    select(
                        PnlProductWhitelist.iiko_product_guid,
                        PnlProductWhitelist.product_name,
                    ).where(PnlProductWhitelist.iiko_product_guid.in_(product_guids))
                )
            ).all()
        }
        observation_names = {
            (item.source_kind, item.iiko_product_guid): item.product_name
            for item in goods.observations
            if item.product_name
        }
        line_by_metric = {metric: line for line, metric in WHITELIST_METRIC.items()}
        for detail in goods.details:
            line_code = line_by_metric.get(detail.metric_code)
            if line_code is None:
                continue
            session.add(
                PnlIikoGoodsFact(
                    period_month=start,
                    metric_code=detail.metric_code,
                    line_code=line_code,
                    source_kind=detail.source_kind,
                    iiko_product_guid=detail.iiko_product_guid,
                    product_name=(
                        observation_names.get((detail.source_kind, detail.iiko_product_guid))
                        or product_names.get(detail.iiko_product_guid)
                    ),
                    amount=detail.amount,
                    rows_count=detail.rows_count,
                )
            )
        await session.flush()

        if goods.menu_snapshot_complete and goods.refreshed_sources == {
            "inventory",
            "incoming_invoice",
        }:
            # Импорты локальные намеренно: ledgers использует константы этого модуля, поэтому
            # импорт на уровне файла создал бы цикл. К моменту вызова iiko_sync уже загружен.
            from app.services.pnl.goods_classifier import auto_classify_new_goods
            from app.services.pnl.ledgers import rebuild_goods_from_observations

            automatic = await auto_classify_new_goods(
                session,
                month_start=start,
                month_end=end_exclusive - dt.timedelta(days=1),
                observations=goods.observations,
                products_payload=goods.products_payload,
                charts_payload=goods.charts_payload,
                settings=get_settings(),
            )
            if automatic.changed or automatic.unresolved:
                result.changed.append(automatic.summary())
            result.changed.extend(automatic.model_errors)
        # Любая свежая выгрузка должна пересобрать суммы уже по текущим правилам, даже если
        # LLM не создала нового правила. Иначе новые остатки/накладные будут ждать ещё сутки.
        from app.services.pnl.ledgers import rebuild_goods_from_observations

        for source_kind in goods.refreshed_sources:
            await rebuild_goods_from_observations(session, start, source_kind)
        for metric_code in set(WHITELIST_METRIC.values()) | {
            METRIC_STOCK_CONSUMPTION,
            METRIC_STOCK_CLOSING,
        }:
            refreshed = await session.scalar(
                select(PnlIikoFact).where(
                    PnlIikoFact.period_month == start,
                    PnlIikoFact.metric_code == metric_code,
                    PnlIikoFact.direction == "total",
                )
            )
            if refreshed is not None:
                result.facts[(metric_code, "total")] = refreshed.amount
    await session.flush()
    return result


# ── Товарные строки: списания, складские остатки, приходные накладные ────────────────

WRITEOFF_ENDPOINT = "/v2/documents/writeoff"
STOCK_BALANCE_ENDPOINT = "/v2/reports/balance/stores"
INVENTORY_ENDPOINT = "/reports/storeOperations"
INCOMING_INVOICE_ENDPOINT = "/documents/export/incomingInvoice"
PRODUCTS_ENDPOINT = "/v2/entities/products/list"
ASSEMBLY_CHARTS_ENDPOINT = "/v2/assemblyCharts/getAll"

PROCESSED_STATUS = "PROCESSED"

METRIC_WRITEOFF = "writeoff_cost"
METRIC_STOCK_CONSUMPTION = "stock_consumption"
METRIC_STOCK_CLOSING = "stock_closing_balance"
METRIC_SHOP_MAINTENANCE = "shop_maintenance_invoices"
METRIC_AUX_GOODS = "aux_goods_invoices"
METRIC_PACKAGING = "packaging_result"
METRIC_PIZZA_BOX = "pizza_box_result"

#: Корзина whitelist → метрика зеркала.
WHITELIST_METRIC = {
    "shop_maintenance": METRIC_SHOP_MAINTENANCE,
    "aux_goods": METRIC_AUX_GOODS,
    "packaging_inventory": METRIC_PACKAGING,
    "pizza_box_inventory": METRIC_PIZZA_BOX,
}

#: Корзины, которые считаются ПО ПРИХОДНЫМ НАКЛАДНЫМ (закупка).
INVOICE_BASKETS = frozenset({METRIC_SHOP_MAINTENANCE, METRIC_AUX_GOODS})

#: Метрика зеркала → эндпоинт, который её посчитал. Пишется в ``source_ref``.
GOODS_METRIC_SOURCE = {
    METRIC_WRITEOFF: WRITEOFF_ENDPOINT,
    METRIC_STOCK_CONSUMPTION: STOCK_BALANCE_ENDPOINT,
    METRIC_STOCK_CLOSING: STOCK_BALANCE_ENDPOINT,
    METRIC_SHOP_MAINTENANCE: INCOMING_INVOICE_ENDPOINT,
    METRIC_AUX_GOODS: INCOMING_INVOICE_ENDPOINT,
    METRIC_PACKAGING: INVENTORY_ENDPOINT,
    METRIC_PIZZA_BOX: INVENTORY_ENDPOINT,
}

#: Корзины, которые считаются ПО РЕЗУЛЬТАТУ ИНВЕНТАРИЗАЦИИ — расхождению книги с фактом.
#:
#: ЭТО НЕ ТО ЖЕ САМОЕ, ЧТО СКЛАДСКОЙ ROLL-FORWARD, и подмена одного другим уже случилась.
#: ``stock_consumption`` = начало + приход − конец = РАСХОД ЗА ПЕРИОД, то есть ровно то, что
#: считает фудкост; отдельной строкой ОПиУ он идти не может ни при каком раскладе. Строка
#: эталона «Результаты инвентаризации упаковки» мерит ПОТЕРЮ: сколько должно быть по учёту
#: против того, что пересчитали. Владелец сформулировал это 04.08.2026 одной фразой — «это по
#: факту фудкост, это расход за весь период, а не разница между фактом и книгой».
#:
#: Когда обе строки выключили в пользу roll-forward, из июля молча ушёл расход 21 018,27 ₽
#: (недостача упаковки 28 308,86 минус излишек коробок 7 290,59), а замены не появилось:
#: потребление уже посчитано фудкостом, потери не считались нигде.
INVENTORY_BASKETS = frozenset({METRIC_PACKAGING, METRIC_PIZZA_BOX})

_CENTS = Decimal("0.01")


@dataclass(slots=True)
class GoodsMetricDetail:
    metric_code: str
    source_kind: str
    iiko_product_guid: str
    amount: Decimal
    rows_count: int


@dataclass(slots=True)
class GoodsProductObservation:
    source_kind: str
    iiko_product_guid: str
    product_name: str | None
    product_code: str | None
    amount: Decimal
    rows_count: int


@dataclass(slots=True)
class GoodsStockFact:
    iiko_product_guid: str
    product_name: str | None
    product_code: str | None
    opening_quantity: Decimal
    opening_amount: Decimal
    receipts_amount: Decimal
    closing_quantity: Decimal
    closing_amount: Decimal
    consumption_amount: Decimal
    stores_count: int


@dataclass(slots=True)
class GoodsSyncResult:
    metrics: dict[str, Decimal] = field(default_factory=dict)
    details: list[GoodsMetricDetail] = field(default_factory=list)
    observations: list[GoodsProductObservation] = field(default_factory=list)
    stock_facts: list[GoodsStockFact] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    refreshed_sources: set[str] = field(default_factory=set)
    products_payload: Any = None
    charts_payload: Any = None
    menu_snapshot_complete: bool = False


async def load_whitelist(
    session: AsyncSession,
    source_kind: str,
    month: dt.date | None = None,
) -> dict[str, str]:
    """Идентификатор товара → метрика. Берём размеченные строкой позиции.

    Позиции ``requires_owner_review`` в расчёт НЕ идут: по ним суммы iiko не сошлись с
    контрольной расшифровкой, и включить их значило бы тихо принять спорную цифру. Они
    показываются отдельно как «не отнесено» — пробел должен быть виден.

    ``stocked`` берётся НАРАВНЕ с ``include``, если у позиции есть ``line_code``. Статус и
    строка отвечают на разные вопросы: первый — участвует ли товар в складском контуре
    (нужно балансу), вторая — в какую строку ОПиУ идёт его инвентаризационное расхождение.
    Коробка для пиццы одновременно и лежит на складе, и имеет свою строку эталона; фильтр
    только по ``include`` вычёркивал её из отчёта вместе со всей упаковкой.
    """
    from app.models.pnl import PnlProductWhitelist
    from app.services.pnl.revision_products import (
        load_revision_product_guids,
        normalize_iiko_product_guid,
    )

    rows = (
        await session.execute(
            select(PnlProductWhitelist.iiko_product_guid, PnlProductWhitelist.line_code).where(
                PnlProductWhitelist.include_status.in_(("include", "stocked")),
                PnlProductWhitelist.source_kind == source_kind,
                PnlProductWhitelist.line_code.is_not(None),
            )
        )
    ).all()
    temporary_guids: set[str] = set()
    if month is not None:
        temporary_guids = set(
            (
                await session.execute(
                    select(PnlProductMonthlyDecision.iiko_product_guid).where(
                        PnlProductMonthlyDecision.period_month == month.replace(day=1),
                        PnlProductMonthlyDecision.decision_kind == "workup",
                    )
                )
            ).scalars()
        )
    blocked_guids = await load_revision_product_guids(session)
    if source_kind != "inventory":
        # Складской товар не может быть ПРЯМЫМ расходом накладной: его потребление уже
        # посчитано складским контуром, и взять ещё и закупку значило бы задвоить.
        #
        # К самой инвентаризации этот запрет неприменим и раньше отменял всю правку целиком.
        # Упаковка одновременно ЛЕЖИТ НА СКЛАДЕ (``stocked``, нужно балансу) и ИМЕЕТ СВОЮ
        # СТРОКУ эталона, куда идёт её инвентаризационное расхождение. Общий блок гасил
        # ровно те 25 позиций, которым 0263 вернула ``line_code``, и следующий же синк
        # записал бы в обе строки 0,00 ₽ — молча и правдоподобно.
        stocked_guids = (
            (
                await session.execute(
                    select(PnlProductWhitelist.iiko_product_guid).where(
                        PnlProductWhitelist.source_kind == "inventory",
                        PnlProductWhitelist.include_status == "stocked",
                    )
                )
            )
            .scalars()
            .all()
        )
        blocked_guids.update(normalize_iiko_product_guid(guid) for guid in stocked_guids)
    return {
        guid: WHITELIST_METRIC[line]
        for guid, line in rows
        if line is not None
        and line in WHITELIST_METRIC
        and guid not in temporary_guids
        and normalize_iiko_product_guid(guid) not in blocked_guids
    }


def writeoff_total(payload: Any) -> Decimal:
    """Сумма проведённых актов списания за период.

    Только ``PROCESSED``: черновик акта — это ещё не списание, товар физически на месте.
    """
    import json

    if isinstance(payload, (bytes, str)):
        payload = json.loads(payload)
    documents = payload.get("response") if isinstance(payload, dict) else payload
    if not isinstance(documents, list):
        return Decimal("0.00")
    total = Decimal("0.00")
    for document in documents:
        if not isinstance(document, dict):
            continue
        if str(document.get("status") or "").upper() != PROCESSED_STATUS:
            continue
        for item in document.get("items") or []:
            if isinstance(item, dict):
                total += _number(item.get("cost"))
    return total


def whitelist_totals(
    rows: list[dict[str, Any]], whitelist: dict[str, str], amount_field: str
) -> dict[str, Decimal]:
    """Свернуть товарные строки по корзинам whitelist.

    Знак ПОЗИЦИЙ сохраняется: складываем подписанные суммы, а не модули. Иначе излишки и
    недостачи схлопнутся друг о друга — по мартовскому контролю нетто вышло −28 104,92 ₽
    против 34 267,48 ₽ суммы модулей.

    Знак СТРОКИ ОТЧЁТА — не забота этого файла. Зеркало хранит ровно то, что ответил iiko
    (минус = недостача), иначе сверка с источником перестанет сходиться; в соглашение отчёта
    величина переводится один раз на границе «зеркало → строка», в
    ``projector.INVERTED_IIKO_METRICS``. Раньше эти два разных «сохранения знака» были
    записаны одной фразой, и строка «Инвентаризация упаковки» показывала недостачу доходом.
    """
    totals: dict[str, Decimal] = {}
    for row in rows:
        guid = str(_field(row, "product", "productId", "iiko_product_guid") or "").strip()
        metric = whitelist.get(guid)
        if metric is None:
            continue
        totals[metric] = totals.get(metric, Decimal("0.00")) + _number(_field(row, amount_field))
    return totals


def whitelist_details(
    rows: list[dict[str, Any]],
    whitelist: dict[str, str],
    amount_field: str,
    *,
    source_kind: str,
) -> list[GoodsMetricDetail]:
    """Свернуть строки iiko по номенклатуре, не теряя вклад отдельных товаров."""
    grouped: dict[tuple[str, str], tuple[Decimal, int]] = {}
    for row in rows:
        guid = str(_field(row, "product", "productId", "iiko_product_guid") or "").strip()
        metric = whitelist.get(guid)
        if metric is None:
            continue
        key = (metric, guid)
        amount, count = grouped.get(key, (Decimal("0.00"), 0))
        grouped[key] = (amount + _number(_field(row, amount_field)), count + 1)
    return [
        GoodsMetricDetail(
            metric_code=metric,
            source_kind=source_kind,
            iiko_product_guid=guid,
            amount=amount.quantize(_CENTS),
            rows_count=count,
        )
        for (metric, guid), (amount, count) in sorted(grouped.items())
    ]


def _product_records(payload: Any) -> list[dict[str, Any]]:
    """Развернуть ответ справочника товаров iiko независимо от обёртки версии API."""
    import json

    if isinstance(payload, (bytes, str)):
        payload = json.loads(payload)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("rows", "data", "items", "records", "result", "products"):
            if key in payload:
                return _product_records(payload[key])
    return []


def product_catalog(payload: Any) -> dict[str, tuple[str | None, str | None]]:
    result: dict[str, tuple[str | None, str | None]] = {}
    for row in _product_records(payload):
        guid = str(_field(row, "id", "product", "productId") or "").strip()
        if not guid:
            continue
        name = str(_field(row, "name", "productName") or "").strip() or None
        code = str(_field(row, "code", "num") or "").strip() or None
        result[guid] = (name, code)
    return result


def product_observations(
    rows: list[dict[str, Any]],
    amount_field: str,
    *,
    source_kind: str,
    catalog: dict[str, tuple[str | None, str | None]],
) -> list[GoodsProductObservation]:
    """Свернуть ВСЕ товары источника, включая ещё не размеченные."""
    grouped: dict[str, tuple[Decimal, int, str | None, str | None]] = {}
    for row in rows:
        guid = str(_field(row, "product", "productId", "iiko_product_guid") or "").strip()
        if not guid:
            continue
        catalog_name, catalog_code = catalog.get(guid, (None, None))
        row_name = str(_field(row, "productName", "product.name", "name") or "").strip()
        row_code = str(_field(row, "productCode", "product.code", "code") or "").strip()
        amount, count, known_name, known_code = grouped.get(
            guid,
            (Decimal("0.00"), 0, None, None),
        )
        grouped[guid] = (
            amount + _number(_field(row, amount_field)),
            count + 1,
            row_name or known_name or catalog_name,
            row_code or known_code or catalog_code,
        )
    return [
        GoodsProductObservation(
            source_kind=source_kind,
            iiko_product_guid=guid,
            product_name=name,
            product_code=code,
            amount=amount.quantize(_CENTS),
            rows_count=count,
        )
        for guid, (amount, count, name, code) in sorted(grouped.items())
    ]


def build_stock_facts(
    opening_rows: list[dict[str, Any]],
    closing_rows: list[dict[str, Any]],
    receipt_observations: list[GoodsProductObservation],
    *,
    catalog: dict[str, tuple[str | None, str | None]],
) -> list[GoodsStockFact]:
    """Собрать расход ``начало + приходы − конец`` по каждому GUID.

    Снимки агрегируются по всем складам, поэтому внутренние перемещения не меняют общий
    актив. Приходы берутся из проведённых входящих накладных того же периода.
    """

    def snapshot(
        rows: list[dict[str, Any]],
    ) -> dict[str, tuple[Decimal, Decimal, set[str]]]:
        grouped: dict[str, tuple[Decimal, Decimal, set[str]]] = {}
        for row in rows:
            guid = str(_field(row, "product", "productId", "iiko_product_guid") or "").strip()
            if not guid:
                continue
            quantity, amount, stores = grouped.get(
                guid,
                (Decimal("0.00"), Decimal("0.00"), set()),
            )
            store = str(_field(row, "store", "storeId", "warehouse") or "").strip()
            grouped[guid] = (
                quantity + _number(_field(row, "amount", "quantity")),
                amount + _number(_field(row, "sum", "cost")),
                stores | ({store} if store else set()),
            )
        return grouped

    opening = snapshot(opening_rows)
    closing = snapshot(closing_rows)
    receipts = {item.iiko_product_guid: item for item in receipt_observations}
    facts: list[GoodsStockFact] = []
    for guid in sorted(set(opening) | set(closing) | set(receipts)):
        opening_quantity, opening_amount, opening_stores = opening.get(
            guid,
            (Decimal("0.00"), Decimal("0.00"), set()),
        )
        closing_quantity, closing_amount, closing_stores = closing.get(
            guid,
            (Decimal("0.00"), Decimal("0.00"), set()),
        )
        receipt = receipts.get(guid)
        receipts_amount = receipt.amount if receipt is not None else Decimal("0.00")
        catalog_name, catalog_code = catalog.get(guid, (None, None))
        facts.append(
            GoodsStockFact(
                iiko_product_guid=guid,
                product_name=(receipt.product_name if receipt is not None else None)
                or catalog_name,
                product_code=(receipt.product_code if receipt is not None else None)
                or catalog_code,
                opening_quantity=opening_quantity,
                opening_amount=opening_amount.quantize(_CENTS),
                receipts_amount=receipts_amount.quantize(_CENTS),
                closing_quantity=closing_quantity,
                closing_amount=closing_amount.quantize(_CENTS),
                consumption_amount=(opening_amount + receipts_amount - closing_amount).quantize(
                    _CENTS
                ),
                stores_count=len(opening_stores | closing_stores),
            )
        )
    return facts


def stock_observations(facts: list[GoodsStockFact]) -> list[GoodsProductObservation]:
    """Представить roll-forward как источник выбора в единой номенклатурной строке."""
    return [
        GoodsProductObservation(
            source_kind="inventory",
            iiko_product_guid=item.iiko_product_guid,
            product_name=item.product_name,
            product_code=item.product_code,
            amount=item.consumption_amount,
            rows_count=item.stores_count,
        )
        for item in facts
        if item.stores_count > 0
    ]


async def _fetch_raw(endpoint: str, params: dict[str, str]) -> Any:
    """Запрос к iiko Server синхронным клиентом в отдельном треде."""

    def _call() -> Any:
        export_employees = _load_export_employees_module()
        export_employees.load_local_env()
        client = export_employees.IikoClient()
        _status, data = _request_iiko_with_incomplete_read_retry(client, endpoint, params=params)
        return data

    return await anyio.to_thread.run_sync(_call)


async def _fetch_stock_balance_rows(timestamp: dt.datetime) -> list[dict[str, Any]]:
    """Стоимостные и количественные остатки всех складов на точную границу периода."""
    data = await _fetch_raw(
        STOCK_BALANCE_ENDPOINT,
        {"timestamp": timestamp.strftime("%Y-%m-%dT%H:%M:%S")},
    )
    return _parse_rows(data)


async def _fetch_inventory_rows(start: dt.date, last: dt.date) -> list[dict[str, Any]]:
    """Товарные строки инвентаризаций периода. Ответ XML — парсер общий с загрузчиком ревизий.

    Запрос отфильтрован по ``documentTypes=INCOMING_INVENTORY`` внутри ``inventory_params``:
    приходные накладные и акты списания приходят другими эндпоинтами, и складского оборота
    в этом ответе нет — только расхождения инвентаризации.
    """

    def _call() -> list[dict[str, Any]]:
        from app.services.iiko_inventory import _load_inventory_module

        inventory = _load_inventory_module()
        inventory.load_local_env()
        client = inventory.IikoClient()
        params = inventory.inventory_params(start, last)
        _status, data = client.request(INVENTORY_ENDPOINT, params=params)
        _format, rows = inventory.parse_store_operations(data)
        return [row for row in rows if isinstance(row, dict)]

    return await anyio.to_thread.run_sync(_call)


async def _fetch_invoice_rows(start: dt.date, last: dt.date) -> list[dict[str, Any]]:
    """Товарные строки приходных накладных периода. Берём только проведённые документы."""

    def _call() -> list[dict[str, Any]]:
        import importlib
        import sys
        from pathlib import Path

        from app.services.iiko_inventory import _candidate_project_roots

        for root in _candidate_project_roots():
            script_dir = Path(root) / "integrations/iiko/scripts"
            if (script_dir / "build_pnl_from_invoices.py").exists():
                if str(script_dir) not in sys.path:
                    sys.path.insert(0, str(script_dir))
                module = importlib.import_module("build_pnl_from_invoices")
                break
        else:
            raise RuntimeError("build_pnl_from_invoices.py недоступен")

        module.load_local_env()
        client = module.IikoClient()
        _status, data = client.request(
            INCOMING_INVOICE_ENDPOINT,
            params={"from": start.isoformat(), "to": last.isoformat()},
        )
        payload = module.xml_payload(data)
        rows: list[dict[str, Any]] = []
        for document in payload.get("document") or []:
            if str(document.get("status") or "").upper() != PROCESSED_STATUS:
                continue
            for item in document.get("item") or []:
                if isinstance(item, dict):
                    rows.append(item)
        return rows

    return await anyio.to_thread.run_sync(_call)


async def fetch_goods_metrics(session: AsyncSession, month: dt.date) -> GoodsSyncResult:
    """Списания, складской roll-forward и товары из приходных накладных за месяц.

    Замечание вместо исключения: если один эндпоинт не ответил, остальные метрики
    залить всё равно нужно — иначе одна недоступность оставляет месяц вовсе без товарных строк.
    Складской расход обновляем только когда получены обе границы остатков И все проведённые
    приходные накладные: без любого из трёх слагаемых формула дала бы правдоподобную ложь.
    """
    start = month.replace(day=1)
    _, following = month_bounds_exclusive(month)
    last = following - dt.timedelta(days=1)
    result = GoodsSyncResult()
    catalog: dict[str, tuple[str | None, str | None]] = {}

    # Имена и коды нужны именно для очереди неизвестных товаров. Недоступность справочника
    # не блокирует цифры: GUID всё равно сохраняется, имя подтянется на следующем прогоне.
    try:
        result.products_payload = await _fetch_raw(PRODUCTS_ENDPOINT, {"includeDeleted": "true"})
        catalog = product_catalog(result.products_payload)
    except Exception as error:  # noqa: BLE001
        result.notes.append(f"Справочник номенклатуры не получен: {error}")

    # Для автоматического выбора источника нужен не только флаг блюда
    # defaultIncludedInMenu, но и рекурсивная цепочка его техкарты до товара. Обе части
    # считаются одним снимком: если хоть одна не загрузилась, автоматика не угадывает.
    try:
        result.charts_payload = await _fetch_raw(
            ASSEMBLY_CHARTS_ENDPOINT,
            {
                "dateFrom": start.isoformat(),
                "dateTo": last.isoformat(),
                "includeDeletedProducts": "true",
                "includePreparedCharts": "true",
            },
        )
    except Exception as error:  # noqa: BLE001
        result.notes.append(f"Техкарты для авторазметки не получены: {error}")
    result.menu_snapshot_complete = bool(catalog) and result.charts_payload is not None

    # Акты списания: даты ISO, только проведённые.
    try:
        payload = await _fetch_raw(
            WRITEOFF_ENDPOINT,
            {
                "dateFrom": start.isoformat(),
                "dateTo": last.isoformat(),
                "status": PROCESSED_STATUS,
            },
        )
        result.metrics[METRIC_WRITEOFF] = writeoff_total(payload)
    except Exception as error:  # noqa: BLE001 — недоступность одного эндпоинта не должна ронять синк
        result.notes.append(f"Акты списания не получены: {error}")

    # Приходные накладные: параметры называются from/to (не dateFrom/dateTo), формат ISO —
    # на дд.мм.гггг эндпоинт отвечает 409. Ответ XML СО СВОЕЙ структурой: документы с
    # вложенными item, а не плоские строки отчёта, поэтому парсер отдельный от инвентаризации.
    invoice_observations: list[GoodsProductObservation] | None = None
    try:
        rows = await _fetch_invoice_rows(start, last)
        invoice_observations = product_observations(
            rows,
            "sum",
            source_kind="incoming_invoice",
            catalog=catalog,
        )
        result.observations.extend(invoice_observations)
        invoice_wl = await load_whitelist(session, "incoming_invoice", start)
        details = whitelist_details(
            rows,
            invoice_wl,
            "sum",
            source_kind="incoming_invoice",
        )
        result.details.extend(details)
        result.refreshed_sources.add("incoming_invoice")
        for metric in INVOICE_BASKETS:
            result.metrics[metric] = sum(
                (detail.amount for detail in details if detail.metric_code == metric),
                Decimal("0.00"),
            )
    except Exception as error:  # noqa: BLE001
        result.notes.append(f"Приходные накладные не получены: {error}")

    # Результат инвентаризации по двум корзинам упаковки — РАСХОЖДЕНИЕ КНИГИ С ФАКТОМ, а не
    # оборот. Живёт отдельным запросом рядом с roll-forward, потому что отвечает на другой
    # вопрос: не «сколько израсходовали», а «сколько потеряли». Первое уже посчитано
    # фудкостом, второе не считается больше нигде.
    # ТОЛЬКО ИТОГИ, БЕЗ ``result.details``, и это не экономия, а необходимость. Детализация
    # ``PnlIikoGoodsFact`` привязана к ``source_kind``, а ``rebuild_goods_from_observations``
    # пересобирает её из наблюдений: для источника ``inventory`` наблюдения приходят из
    # roll-forward и несут ПОТРЕБЛЕНИЕ, а не расхождение. Записанные здесь строки удалялись
    # бы той же транзакцией (``ledgers.GOODS_LINES_BY_SOURCE['inventory']`` пуст), а если
    # добавить их в пересборку — в детализацию встал бы складской оборот вместо недостачи.
    # Итог метрики от этого не страдает: его пишет цикл по ``result.metrics``.
    try:
        inventory_rows = await _fetch_inventory_rows(start, last)
        inventory_wl = await load_whitelist(session, "inventory", start)
        inventory_details = whitelist_details(
            inventory_rows,
            inventory_wl,
            "sum",
            source_kind="inventory",
        )
        for metric in INVENTORY_BASKETS:
            result.metrics[metric] = sum(
                (detail.amount for detail in inventory_details if detail.metric_code == metric),
                Decimal("0.00"),
            )
    except Exception as error:  # noqa: BLE001
        result.notes.append(f"Результаты инвентаризации упаковки не получены: {error}")

    # Не складываем результаты отдельных еженедельных ревизий. Берём два точных снимка
    # всех складов и считаем фактический расход товара за весь месяц. Так в расчёт попадают
    # и недостачи, и излишки, а внутренние перемещения между складами взаимно уничтожаются.
    if invoice_observations is not None:
        try:
            opening_rows = await _fetch_stock_balance_rows(dt.datetime.combine(start, dt.time.min))
            closing_rows = await _fetch_stock_balance_rows(
                dt.datetime.combine(following, dt.time.min)
            )
            result.stock_facts = build_stock_facts(
                opening_rows,
                closing_rows,
                invoice_observations,
                catalog=catalog,
            )
            result.observations.extend(stock_observations(result.stock_facts))
            result.refreshed_sources.add("inventory")
        except Exception as error:  # noqa: BLE001
            result.notes.append(f"Складские остатки на границах месяца не получены: {error}")
    else:
        result.notes.append(
            "Складской расход не обновлён: без приходных накладных нельзя вычислить "
            "начало + приход − конец"
        )

    return result

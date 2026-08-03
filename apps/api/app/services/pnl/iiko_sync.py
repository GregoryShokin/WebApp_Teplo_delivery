"""Синхронизация месячных фактов iiko в зеркало ОПиУ.

ЧТО ТЯНЕМ. Выручку без скидок и со скидками, себестоимость продаж (фудкост) — по направлениям,
и зарплату курьеров. Всё остальное в отчёте живёт внутри приложения.

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
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pnl import PnlIikoFact
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
class SyncResult:
    """Что синк сделал за месяц — для лога и для предупреждений в отчёте."""

    month: dt.date
    facts: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    unmapped_directions: set[str] = field(default_factory=set)
    rows_seen: int = 0
    changed: list[str] = field(default_factory=list)


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


async def sync_month(session: AsyncSession, month: dt.date) -> SyncResult:
    """Залить месячные факты iiko в зеркало. Идемпотентно: повторный прогон обновляет."""
    await _load_source_credential_env(session)
    start, end_exclusive = month_bounds_exclusive(month)

    revenue_rows = await _fetch_preset(REVENUE_PRESET_ID, start, end_exclusive)
    result = aggregate_revenue_rows(revenue_rows)
    result.month = start

    courier_rows = await _fetch_preset(PNL_PRESET_ID, start, end_exclusive)
    result.facts[(METRIC_COURIER_SALARY, "total")] = courier_salary_from_rows(courier_rows)

    for (metric, direction), amount in sorted(result.facts.items()):
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
                    rows_count=result.rows_seen,
                    source_ref=REVENUE_PRESET_ID
                    if metric != METRIC_COURIER_SALARY
                    else PNL_PRESET_ID,
                )
            )
            continue
        if existing.amount != amount:
            # Цифра закрытого месяца изменилась. Молча переписать значило бы стереть след:
            # прошлое значение сохраняем и сообщаем наружу.
            result.changed.append(f"{metric}/{direction}: было {existing.amount}, стало {amount}")
            existing.previous_amount = existing.amount
        existing.amount = amount
        existing.rows_count = result.rows_seen
    await session.flush()
    return result

"""Дашборд скорости кухни: 4 интервала медиана/p90 по часам, ASAP/предзаказ, KPI.

Интервалы (мин): created→Готово (очередь+готовка) · Готово→Ждёт курьера (упаковка) ·
Ждёт курьера→Передан (ожидание курьера) · Передан→Доставлен (дорога). «Доставлен» — из
снимка ``kds_order.delivered_at`` (whenDelivered), иначе из OLAP ``delivery_order``
(join по iiko GUID, fallback по (work_date, order_number)). Перцентили — чистый Python.
"""

from __future__ import annotations

import statistics
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DeliveryOrder, KdsOrder
from app.services.settings_service import SettingNotFoundError, get_setting_model

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
HOT_LATE_TOLERANCE_SETTING_KEY = "kds.hot_late_tolerance_minutes"
PEAK_HOURS = range(16, 20)  # 16:00–19:59
INTERVALS = ("queue_cook", "packing", "courier_wait", "road")
SEGMENTS = ("asap", "preorder")


def _pct(values: list[float], p: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = int(round((p / 100) * (len(ordered) - 1)))
    return round(ordered[k], 1)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return round(statistics.median(values), 1)


def _minutes(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    delta = (end - start).total_seconds() / 60
    return delta if delta >= 0 else None  # отрицательные = рассинхрон часов, отбрасываем


def _stat_block(values: list[float]) -> dict[str, float | int | None]:
    return {"median": _median(values), "p90": _pct(values, 90), "n": len(values)}


async def _read_hot_late_tolerance(session: AsyncSession) -> int:
    try:
        setting = await get_setting_model(session, HOT_LATE_TOLERANCE_SETTING_KEY)
    except SettingNotFoundError:
        return 0
    try:
        return int(setting.value)
    except (TypeError, ValueError):
        return 0


async def compute_dashboard(
    session: AsyncSession,
    *,
    date_from: date,
    date_to: date,
) -> dict[str, object]:
    tolerance = await _read_hot_late_tolerance(session)

    kds_rows = (
        await session.scalars(
            select(KdsOrder).where(
                KdsOrder.work_date >= date_from, KdsOrder.work_date <= date_to
            )
        )
    ).all()

    delivery_rows = (
        await session.scalars(
            select(DeliveryOrder).where(
                DeliveryOrder.work_date >= date_from, DeliveryOrder.work_date <= date_to
            )
        )
    ).all()
    delivered_by_id: dict[str, datetime] = {}
    delivered_by_number: dict[tuple[date, str], datetime] = {}
    for row in delivery_rows:
        if row.delivered_at is None:
            continue
        if row.iiko_order_id:
            delivered_by_id[row.iiko_order_id] = row.delivered_at
        if row.order_number:
            delivered_by_number[(row.work_date, str(row.order_number))] = row.delivered_at

    def resolve_delivered(order: KdsOrder) -> datetime | None:
        if order.delivered_at is not None:
            return order.delivered_at
        by_id = delivered_by_id.get(order.iiko_order_id)
        if by_id is not None:
            return by_id
        if order.order_number is not None:
            return delivered_by_number.get((order.work_date, str(order.order_number)))
        return None

    # buckets[(segment, hour)][interval] -> list[float]
    buckets: dict[tuple[str, int], dict[str, list[float]]] = {}
    peak: dict[str, list[float]] = {interval: [] for interval in INTERVALS}
    packing_all: list[float] = []
    courier_wait_all: list[float] = []
    hot_late = hot_total = rolls_late = rolls_total = 0
    pre_ready_on_time = pre_ready_total = 0
    pre_delivered_on_time = pre_delivered_total = 0
    orders_total = len(kds_rows)
    orders_with_taps = 0

    for order in kds_rows:
        delivered = resolve_delivered(order)
        values = {
            "queue_cook": _minutes(order.when_created, order.ready_at),
            "packing": _minutes(order.ready_at, order.waiting_courier_at),
            "courier_wait": _minutes(order.waiting_courier_at, order.handed_to_courier_at),
            "road": _minutes(order.handed_to_courier_at, delivered),
        }
        if any(v is not None for v in values.values()):
            orders_with_taps += 1

        segment = "preorder" if order.is_preorder else "asap"
        hour = (
            order.when_created.astimezone(MOSCOW_TZ).hour
            if order.when_created is not None
            else None
        )
        if hour is not None:
            slot = buckets.setdefault((segment, hour), {iv: [] for iv in INTERVALS})
            for interval, value in values.items():
                if value is not None:
                    slot[interval].append(value)
                    if segment == "asap" and hour in PEAK_HOURS:
                        peak[interval].append(value)

        if values["packing"] is not None:
            packing_all.append(values["packing"])
        if values["courier_wait"] is not None:
            courier_wait_all.append(values["courier_wait"])

        # KPI опоздания горячих vs роллов (по факту доставки vs обещание)
        if delivered is not None and order.complete_before is not None:
            late = (delivered - order.complete_before).total_seconds() / 60 > tolerance
            if order.has_hot_item:
                hot_total += 1
                hot_late += 1 if late else 0
            else:
                rolls_total += 1
                rolls_late += 1 if late else 0

        # Пунктуальность предзаказов (а не «скорость»)
        if order.is_preorder and order.complete_before is not None:
            if order.ready_at is not None:
                pre_ready_total += 1
                pre_ready_on_time += 1 if order.ready_at <= order.complete_before else 0
            if delivered is not None:
                pre_delivered_total += 1
                pre_delivered_on_time += 1 if delivered <= order.complete_before else 0

    by_hour = []
    for (segment, hour), slot in sorted(buckets.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        entry: dict[str, object] = {
            "hour": hour,
            "segment": segment,
            "count": max(len(slot[iv]) for iv in INTERVALS),
        }
        for interval in INTERVALS:
            entry[interval] = _stat_block(slot[interval])
        by_hour.append(entry)

    def _pct_ratio(numerator: int, denominator: int) -> float | None:
        return round(100 * numerator / denominator, 1) if denominator else None

    return {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "segments": list(SEGMENTS),
        "intervals": list(INTERVALS),
        "by_hour": by_hour,
        "peak": {
            "hours": "16:00–19:59",
            "count": max((len(peak[iv]) for iv in INTERVALS), default=0),
            **{interval: _stat_block(peak[interval]) for interval in INTERVALS},
        },
        "kpi": {
            "median_packing_min": _median(packing_all),
            "median_courier_wait_min": _median(courier_wait_all),
            "hot_late_pct": _pct_ratio(hot_late, hot_total),
            "hot_late_n": hot_total,
            "rolls_late_pct": _pct_ratio(rolls_late, rolls_total),
            "rolls_late_n": rolls_total,
            "orders_total": orders_total,
            "orders_with_taps": orders_with_taps,
        },
        "preorder_punctuality": {
            "ready_on_time_pct": _pct_ratio(pre_ready_on_time, pre_ready_total),
            "ready_n": pre_ready_total,
            "delivered_on_time_pct": _pct_ratio(pre_delivered_on_time, pre_delivered_total),
            "delivered_n": pre_delivered_total,
        },
    }

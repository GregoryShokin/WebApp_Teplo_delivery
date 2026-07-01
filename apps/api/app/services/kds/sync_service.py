"""Наполнение снимка очереди КДС из iikoCloud (вебхук + сверочный поллинг).

``parse_cloud_order`` + ``upsert_kds_order`` — общий путь и для вебхука
``DeliveryOrderUpdate``, и для reconcile-поллинга. Идемпотентность по ``last_event_at``
(старое событие не перетирает свежий снимок). Тап-колонки тут не трогаем — их ведёт
``event_service`` (журнал тапов планшета).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import anyio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KdsOrder
from app.services.iiko_sync import _load_source_credential_env
from app.services.kds.iiko_cloud_client import (
    build_cloud_client,
    discover_organization_id,
)
from app.services.settings_service import SettingNotFoundError, get_setting_model, write_setting

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
ORG_ID_SETTING_KEY = "kds.iiko_cloud_org_id"
PREORDER_HORIZON_SETTING_KEY = "kds.preorder_horizon_minutes"
DEFAULT_PREORDER_HORIZON_MINUTES = 120

# Заказ ушёл из активной очереди КДС.
INACTIVE_STATUSES = frozenset({"Delivered", "Closed", "Cancelled"})

# Горячие позиции (отдельный KPI опоздания vs роллы). product.category в iikoCloud пуст —
# классифицируем эвристикой по названию. Список можно вынести в настройку позже.
HOT_KEYWORDS = (
    "пицц",
    "бургер",
    "шаурм",
    "шаверм",
    "вок",
    "лапш",
    "суп",
    "горяч",
    "картоф",
    "фри",
    "наггетс",
    "крыл",
    "стейк",
    "паста",
    "хачапур",
    "блин",
    "котлет",
    "шашлык",
    "люля",
)


@dataclass(slots=True)
class ParsedKdsOrder:
    iiko_order_id: str
    order_number: str | None
    work_date: date
    status: str | None
    complete_before: datetime | None
    when_created: datetime | None
    delivered_at: datetime | None
    is_preorder: bool
    has_hot_item: bool
    is_active: bool
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class KdsSyncResult:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    deactivated: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped": self.skipped,
            "deactivated": self.deactivated,
        }


def parse_cloud_ts(value: Any) -> datetime | None:
    """iikoCloud отдаёт локальное (МСК) время «YYYY-MM-DD HH:MM:SS.mmm» без таймзоны."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    # запасной разбор ISO (вдруг вебхук отдаёт иначе)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def _to_utc(naive_or_aware: datetime | None) -> datetime | None:
    if naive_or_aware is None:
        return None
    if naive_or_aware.tzinfo is None:
        return naive_or_aware.replace(tzinfo=MOSCOW_TZ).astimezone(UTC)
    return naive_or_aware.astimezone(UTC)


def parse_webhook_event_time(value: Any) -> datetime | None:
    parsed = parse_cloud_ts(value)
    if parsed is None and value not in (None, ""):
        # eventTime может прийти в epoch-секундах/миллисекундах
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number > 1e12:
            number /= 1000.0
        return datetime.fromtimestamp(number, tz=UTC)
    return _to_utc(parsed)


def has_hot_item(order: dict[str, Any]) -> bool:
    for item in order.get("items") or []:
        if item.get("deleted"):
            continue
        product = item.get("product") or {}
        name = str(product.get("name") or "").casefold()
        if any(keyword in name for keyword in HOT_KEYWORDS):
            return True
    return False


def _trim_raw(order: dict[str, Any]) -> dict[str, Any]:
    """Без гостевой PII (телефон/адрес/имя) — храним только нужное для метрик/дебага."""
    return {
        "number": order.get("number"),
        "status": order.get("status"),
        "isAsap": order.get("isAsap"),
        "completeBefore": order.get("completeBefore"),
        "whenCreated": order.get("whenCreated"),
        "whenCookingCompleted": order.get("whenCookingCompleted"),
        "whenPacked": order.get("whenPacked"),
        "whenSended": order.get("whenSended"),
        "whenDelivered": order.get("whenDelivered"),
        "orderServiceType": (order.get("orderType") or {}).get("orderServiceType"),
        "items": [
            (item.get("product") or {}).get("name")
            for item in (order.get("items") or [])
            if not item.get("deleted")
        ],
    }


def parse_cloud_order(
    iiko_order_id: str,
    order: dict[str, Any],
    *,
    preorder_horizon_minutes: int,
) -> ParsedKdsOrder:
    when_created_naive = parse_cloud_ts(order.get("whenCreated"))
    complete_before_naive = parse_cloud_ts(order.get("completeBefore"))
    delivered_naive = parse_cloud_ts(order.get("whenDelivered"))

    work_date = (
        (when_created_naive or complete_before_naive).date()
        if (when_created_naive or complete_before_naive)
        else datetime.now(MOSCOW_TZ).date()
    )

    is_preorder = False
    if when_created_naive is not None and complete_before_naive is not None:
        horizon = (complete_before_naive - when_created_naive).total_seconds() / 60
        is_preorder = horizon > preorder_horizon_minutes

    status = order.get("status")
    return ParsedKdsOrder(
        iiko_order_id=iiko_order_id,
        order_number=str(order["number"]) if order.get("number") is not None else None,
        work_date=work_date,
        status=status,
        complete_before=_to_utc(complete_before_naive),
        when_created=_to_utc(when_created_naive),
        delivered_at=_to_utc(delivered_naive),
        is_preorder=is_preorder,
        has_hot_item=has_hot_item(order),
        is_active=status not in INACTIVE_STATUSES,
        raw=_trim_raw(order),
    )


async def upsert_kds_order(
    session: AsyncSession,
    parsed: ParsedKdsOrder,
    *,
    event_at: datetime | None,
) -> tuple[KdsOrder, bool, bool]:
    """Upsert снимка. Возвращает (order, created, applied). ``event_at`` None = поллинг
    (авторитетный текущий снимок → всегда применяем)."""
    order = await session.scalar(
        select(KdsOrder).where(KdsOrder.iiko_order_id == parsed.iiko_order_id)
    )
    now = datetime.now(UTC)
    effective_event_at = event_at or now
    created = order is None
    if order is None:
        order = KdsOrder(id=uuid.uuid4(), iiko_order_id=parsed.iiko_order_id)
        session.add(order)
    elif order.last_event_at is not None and effective_event_at < order.last_event_at:
        return order, False, False  # out-of-order событие — снимок не трогаем

    order.order_number = parsed.order_number
    order.work_date = parsed.work_date
    order.status = parsed.status
    order.complete_before = parsed.complete_before
    order.when_created = parsed.when_created
    order.delivered_at = parsed.delivered_at
    order.is_preorder = parsed.is_preorder
    order.has_hot_item = parsed.has_hot_item
    order.is_active = parsed.is_active
    order.raw = parsed.raw
    order.last_event_at = effective_event_at
    return order, created, True


async def get_preorder_horizon_minutes(session: AsyncSession) -> int:
    try:
        setting = await get_setting_model(session, PREORDER_HORIZON_SETTING_KEY)
    except SettingNotFoundError:
        return DEFAULT_PREORDER_HORIZON_MINUTES
    try:
        return int(setting.value)
    except (TypeError, ValueError):
        return DEFAULT_PREORDER_HORIZON_MINUTES


async def read_org_id(session: AsyncSession) -> str:
    try:
        setting = await get_setting_model(session, ORG_ID_SETTING_KEY)
    except SettingNotFoundError:
        return ""
    return str(setting.value or "").strip()


def _fetch_active_orders_blocking(
    org_id: str | None,
    date_from: date,
    date_to: date,
) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
    client = build_cloud_client()
    resolved = org_id or discover_organization_id(client)
    if not resolved:
        raise RuntimeError("iikoCloud organizationId не определён")
    body = {
        "organizationIds": [resolved],
        "deliveryDateFrom": f"{date_from.isoformat()} 00:00:00.000",
        "deliveryDateTo": f"{date_to.isoformat()} 00:00:00.000",
    }
    response = client.post("/api/1/deliveries/by_delivery_date_and_status", body)
    orders: list[tuple[str, dict[str, Any]]] = []
    for group in response.get("ordersByOrganizations") or []:
        for wrapper in group.get("orders") or []:
            order = wrapper.get("order")
            order_id = wrapper.get("id") or (order or {}).get("id")
            if order and order_id:
                orders.append((str(order_id), order))
    return resolved, orders


async def poll_active_orders(session: AsyncSession, *, days_ahead: int = 2) -> KdsSyncResult:
    """Сверочный поллинг: тянем сегодняшнее окно (+ближние предзаказы), обновляем снимок,
    гасим зависшие активные заказы прошлых дней."""
    await _load_source_credential_env(session)
    horizon = await get_preorder_horizon_minutes(session)
    org_id = await read_org_id(session)

    today = datetime.now(MOSCOW_TZ).date()
    date_to = today + timedelta(days=days_ahead + 1)
    resolved, orders = await anyio.to_thread.run_sync(
        _fetch_active_orders_blocking, org_id or None, today, date_to
    )
    if not org_id and resolved:
        await write_setting(session, ORG_ID_SETTING_KEY, resolved, changed_by_user_id=None)

    result = KdsSyncResult()
    seen: set[str] = set()
    for order_id, order in orders:
        parsed = parse_cloud_order(order_id, order, preorder_horizon_minutes=horizon)
        _order, created, _applied = await upsert_kds_order(session, parsed, event_at=None)
        seen.add(order_id)
        if created:
            result.inserted += 1
        else:
            result.updated += 1

    # Зависшие: активные заказы прошлых дней (вебхук «доставлен» мог потеряться). НО не
    # гасим заказы, которые этот же поллинг только что подтвердил активными (`seen`) —
    # иначе кросс-полуночный предзаказ (создан вчера, срок сегодня) убивался бы каждые 5 мин.
    stale_query = update(KdsOrder).where(
        KdsOrder.is_active.is_(True), KdsOrder.work_date < today
    )
    if seen:
        stale_query = stale_query.where(KdsOrder.iiko_order_id.notin_(seen))
    stale = await session.execute(stale_query.values(is_active=False))
    result.deactivated = stale.rowcount or 0
    await session.commit()
    return result

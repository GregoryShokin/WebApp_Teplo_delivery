"""Direct OLAP sync for courier deliveries using http.client.

The legacy IikoClient (urllib.request) is blocked by the iiko WAF in some
environments; http.client connects fine. This module is a minimal replacement
that pulls the DELIVERIES OLAP report and upserts into delivery_order.
"""

from __future__ import annotations

import http.client as hc
import json
import logging
import os
import ssl
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DeliveryOrder

logger = logging.getLogger(__name__)

OLAP_FIELDS = [
    "Delivery.Id",
    "Delivery.Number",
    "Delivery.ServiceType",
    "Delivery.Courier",
    "Delivery.Courier.Id",
    "OpenTime",
    "Delivery.SendTime",
    "Delivery.ActualTime",
    "Delivery.CloseTime",
    "Delivery.WayDuration",
    "OpenDate.Typed",
    "Delivery.CancelCause",
]
AGGREGATE_FIELDS = ["DishDiscountSumInt"]


@dataclass(slots=True)
class OlapSyncReport:
    fetched: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "fetched": self.fetched,
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
        }


def _iiko_host_and_port() -> tuple[str, int]:
    base = os.environ.get("IIKO_SERVER_BASE_URL", "").strip()
    if not base:
        raise RuntimeError("IIKO_SERVER_BASE_URL is missing")
    parsed = urllib.parse.urlparse(base if "://" in base else f"https://{base}")
    host = parsed.hostname or ""
    port = parsed.port or 443
    return host, port


def _auth_token() -> str:
    host, port = _iiko_host_and_port()
    login = os.environ.get("IIKO_SERVER_LOGIN", "").strip()
    password_sha1 = os.environ.get("IIKO_SERVER_PASSWORD_SHA1", "").strip()
    if not password_sha1 and (raw := os.environ.get("IIKO_SERVER_PASSWORD", "").strip()):
        import hashlib

        password_sha1 = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    if not login or not password_sha1:
        raise RuntimeError("IIKO_SERVER_LOGIN / PASSWORD env missing")
    ctx = ssl._create_unverified_context()
    conn = hc.HTTPSConnection(host, port, timeout=30, context=ctx)
    try:
        query = urllib.parse.urlencode({"login": login, "pass": password_sha1})
        conn.request("GET", f"/resto/api/auth?{query}")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace").strip()
        if resp.status != 200 or not body or "<" in body or "\n" in body:
            raise RuntimeError(f"iiko auth failed: {resp.status} {body[:200]}")
        return body
    finally:
        conn.close()


def fetch_olap_deliveries(date_from: date, date_to: date) -> list[dict[str, Any]]:
    """Pull DELIVERIES OLAP report for [date_from, date_to] inclusive."""
    token = _auth_token()
    host, port = _iiko_host_and_port()
    body = json.dumps(
        {
            "reportType": "DELIVERIES",
            "buildSummary": False,
            "groupByRowFields": OLAP_FIELDS,
            "groupByColFields": [],
            "aggregateFields": AGGREGATE_FIELDS,
            "filters": {
                "OpenDate.Typed": {
                    "filterType": "DateRange",
                    "periodType": "CUSTOM",
                    "from": f"{date_from.isoformat()}T00:00:00.000",
                    "to": f"{(date_to + timedelta(days=1)).isoformat()}T00:00:00.000",
                    "includeLow": True,
                    "includeHigh": False,
                }
            },
        }
    ).encode("utf-8")
    ctx = ssl._create_unverified_context()
    conn = hc.HTTPSConnection(host, port, timeout=120, context=ctx)
    try:
        conn.request(
            "POST",
            f"/resto/api/v2/reports/olap?key={token}",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"iiko OLAP failed: {resp.status} {raw[:300]!r}")
        return json.loads(raw).get("data", [])
    finally:
        conn.close()


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # iiko OLAP returns naive ISO datetimes in the venue local timezone (MSK).
    # Mark them explicitly so Postgres timestamptz storage is correct.
    try:
        if "T" in text:
            parsed = datetime.fromisoformat(text)
        else:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MOSCOW_TZ)
    return parsed


def _parse_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _derive_status(row: dict[str, Any]) -> str:
    if row.get("Delivery.CancelCause"):
        return "Cancelled"
    if row.get("Delivery.ActualTime") or row.get("Delivery.CloseTime"):
        return "Closed"
    if row.get("Delivery.SendTime"):
        return "OnWay"
    return "Open"


async def sync_courier_olap_deliveries(
    session: AsyncSession,
    *,
    date_from: date,
    date_to: date,
) -> OlapSyncReport:
    """Pull OLAP DELIVERIES and upsert into delivery_order."""
    rows = fetch_olap_deliveries(date_from, date_to)
    report = OlapSyncReport(fetched=len(rows))

    iiko_ids = [r.get("Delivery.Id") for r in rows if r.get("Delivery.Id")]
    existing: dict[str, DeliveryOrder] = {}
    if iiko_ids:
        result = await session.scalars(
            select(DeliveryOrder).where(DeliveryOrder.iiko_order_id.in_(iiko_ids))
        )
        for order in result.all():
            existing[order.iiko_order_id] = order

    for row in rows:
        iiko_id = row.get("Delivery.Id")
        work_date = _parse_date(row.get("OpenDate.Typed"))
        if not iiko_id or not work_date:
            report.skipped += 1
            continue

        send_time = _parse_dt(row.get("Delivery.SendTime"))
        actual_time = _parse_dt(row.get("Delivery.ActualTime"))
        close_time = _parse_dt(row.get("Delivery.CloseTime"))
        open_time = _parse_dt(row.get("OpenTime"))
        way_duration = row.get("Delivery.WayDuration")
        revenue_raw = row.get("DishDiscountSumInt")
        number = row.get("Delivery.Number")

        target_values: dict[str, Any] = {
            "iiko_order_id": iiko_id,
            "order_number": str(number) if number is not None else None,
            "work_date": work_date,
            "status": _derive_status(row),
            "service_type": row.get("Delivery.ServiceType"),
            "courier_iiko_id": row.get("Delivery.Courier.Id"),
            "opened_at": open_time,
            "on_way_at": send_time,
            "closed_at": close_time,
            "taken_at": send_time,
            "delivered_at": actual_time,
            "way_duration_minutes": (
                Decimal(str(way_duration)) if isinstance(way_duration, (int, float)) else None
            ),
            "revenue": (
                Decimal(str(revenue_raw)) if isinstance(revenue_raw, (int, float)) else None
            ),
            "raw": row,
        }

        order = existing.get(iiko_id)
        if order is None:
            order = DeliveryOrder(id=uuid.uuid4(), **target_values)
            session.add(order)
            report.created += 1
        else:
            for key, value in target_values.items():
                setattr(order, key, value)
            report.updated += 1

    await session.flush()
    logger.info(
        "courier olap sync done: fetched=%s created=%s updated=%s skipped=%s",
        report.fetched, report.created, report.updated, report.skipped,
    )
    return report

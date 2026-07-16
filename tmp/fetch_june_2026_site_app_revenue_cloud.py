#!/usr/bin/env python3
"""Aggregate June 2026 website and Starter App revenue via iiko Cloud orders."""

from __future__ import annotations

import csv
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "integrations" / "iiko" / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))

from export_orders_delivery import load_local_env  # noqa: E402
from app.services.iiko_cloud_client import (  # noqa: E402
    IIKO_ORGANIZATION_ID,
    iiko_auth_token,
    iiko_cloud_call,
    iiko_opener,
)


START = dt.datetime(2026, 6, 1)
END = dt.datetime(2026, 7, 1)
PROCESSED_DIR = PROJECT_ROOT / "research" / "processed" / "iiko" / "orders_delivery"
JSON_PATH = PROCESSED_DIR / "site_app_revenue_2026-06_cloud_check.json"
CSV_PATH = PROCESSED_DIR / "site_app_revenue_2026-06_cloud_check.csv"


def format_iiko(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S.000")


def channel_for(source: str) -> str:
    normalized = source.strip().casefold()
    if normalized in {"сайт", "site", "website", "web"}:
        return "website"
    if normalized in {"ios", "андройд", "андроид", "android"}:
        return "starter_app"
    return "other_or_unknown"


def source_name(order: dict[str, Any]) -> str:
    source = order.get("marketingSource")
    if isinstance(source, dict):
        return str(source.get("name") or "").strip() or "(пусто)"
    return "(пусто)"


def main() -> int:
    load_local_env()
    opener = iiko_opener()
    token = iiko_auth_token(opener)

    unique_orders: dict[str, dict[str, Any]] = {}
    chunk_start = START
    while chunk_start < END:
        # The endpoint enforces a fairly small result-size limit for this venue;
        # daily chunks stay below it and are deduplicated by order id below.
        chunk_end = min(chunk_start + dt.timedelta(days=1), END)
        status, payload = iiko_cloud_call(
            "/api/1/deliveries/by_delivery_date_and_status",
            {
                "organizationIds": [IIKO_ORGANIZATION_ID],
                "deliveryDateFrom": format_iiko(chunk_start),
                "deliveryDateTo": format_iiko(chunk_end),
                "statuses": ["Closed"],
            },
            token=token,
            opener=opener,
            timeout=90,
        )
        if status != 200 or not isinstance(payload, dict):
            raise RuntimeError(f"iiko Cloud chunk failed: status={status}")
        for group in payload.get("ordersByOrganizations", []):
            if not isinstance(group, dict):
                continue
            for record in group.get("orders", []):
                if not isinstance(record, dict):
                    continue
                order = record.get("order")
                if not isinstance(order, dict) or order.get("status") != "Closed":
                    continue
                order_id = str(record.get("id") or record.get("posId") or "").strip()
                if order_id:
                    unique_orders[order_id] = order
        chunk_start = chunk_end

    by_source: dict[str, dict[str, float]] = defaultdict(
        lambda: {"orders": 0.0, "revenue": 0.0}
    )
    by_channel: dict[str, dict[str, float]] = defaultdict(
        lambda: {"orders": 0.0, "revenue": 0.0}
    )
    for order in unique_orders.values():
        source = source_name(order)
        channel = channel_for(source)
        revenue = float(order.get("sum") or 0)
        by_source[source]["orders"] += 1
        by_source[source]["revenue"] += revenue
        by_channel[channel]["orders"] += 1
        by_channel[channel]["revenue"] += revenue

    source_rows = [
        {
            "marketing_source": source,
            "channel": channel_for(source),
            "orders": int(metrics["orders"]),
            "revenue": round(metrics["revenue"], 2),
        }
        for source, metrics in sorted(
            by_source.items(), key=lambda item: (-item[1]["revenue"], item[0])
        )
    ]
    channel_rows = [
        {
            "channel": channel,
            "orders": int(metrics["orders"]),
            "revenue": round(metrics["revenue"], 2),
        }
        for channel, metrics in sorted(by_channel.items())
    ]
    payload = {
        "period_start": START.date().isoformat(),
        "period_end": (END.date() - dt.timedelta(days=1)).isoformat(),
        "metric": "closed delivery order.sum (after discounts)",
        "channel_summary": channel_rows,
        "marketing_source_detail": source_rows,
        "unique_closed_orders": len(unique_orders),
    }

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with CSV_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["marketing_source", "channel", "orders", "revenue"]
        )
        writer.writeheader()
        writer.writerows(source_rows)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

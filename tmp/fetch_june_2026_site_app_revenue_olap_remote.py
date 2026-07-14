#!/usr/bin/env python3
"""Run inside the production API container; prints only aggregate iiko OLAP data."""

from __future__ import annotations

import json
import sys
from collections import defaultdict


sys.path.insert(0, "/app/integrations/iiko/scripts")

from export_orders_delivery import (  # noqa: E402
    IikoClient,
    clean_text,
    department_scope,
    orders_from,
    parse_rows,
    revenue_from,
    value_from,
)


def channel_for(source: str) -> str:
    normalized = clean_text(source).casefold()
    if normalized in {"сайт", "site", "website", "web"}:
        return "website"
    if normalized in {"ios", "андройд", "андроид", "android"}:
        return "starter_app"
    return "other_or_unknown"


client = IikoClient()
status, raw = client.request(
    "/reports/olap",
    params={
        "report": "SALES",
        "summary": "false",
        "from": "01.06.2026",
        "to": "30.06.2026",
        "groupRow": ["Department", "Delivery.MarketingSource"],
        "agr": ["OrderNum", "DishSumInt", "DishDiscountSumInt", "DiscountSum"],
    },
)
rows = parse_rows(
    raw,
    [
        "Department",
        "Delivery.MarketingSource",
        "OrderNum",
        "DishSumInt",
        "DishDiscountSumInt",
        "DiscountSum",
    ],
)

by_source = defaultdict(lambda: {"orders": 0.0, "revenue": 0.0})
by_channel = defaultdict(lambda: {"orders": 0.0, "revenue": 0.0})
active_rows = 0
for row in rows:
    department = clean_text(value_from(row, ["Department"]))
    if department_scope(department) != "active_chernikova":
        continue
    active_rows += 1
    source = clean_text(value_from(row, ["Delivery.MarketingSource"])) or "(пусто)"
    channel = channel_for(source)
    orders = orders_from(row)
    revenue = revenue_from(row)
    by_source[source]["orders"] += orders
    by_source[source]["revenue"] += revenue
    by_channel[channel]["orders"] += orders
    by_channel[channel]["revenue"] += revenue

payload = {
    "status": status,
    "period_start": "2026-06-01",
    "period_end": "2026-06-30",
    "metric": "DishDiscountSumInt (revenue after discounts)",
    "channel_summary": [
        {
            "channel": channel,
            "orders": round(values["orders"], 2),
            "revenue": round(values["revenue"], 2),
        }
        for channel, values in sorted(by_channel.items())
    ],
    "marketing_source_detail": [
        {
            "marketing_source": source,
            "channel": channel_for(source),
            "orders": round(values["orders"], 2),
            "revenue": round(values["revenue"], 2),
        }
        for source, values in sorted(
            by_source.items(), key=lambda item: (-item[1]["revenue"], item[0])
        )
    ],
    "raw_rows": len(rows),
    "active_department_rows": active_rows,
}
print(json.dumps(payload, ensure_ascii=False, indent=2))

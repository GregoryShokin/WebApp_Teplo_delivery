#!/usr/bin/env python3
"""Fetch June 2026 site and Starter App revenue from the read-only iiko OLAP report."""

from __future__ import annotations

import csv
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
IIKO_SCRIPTS = PROJECT_ROOT / "integrations" / "iiko" / "scripts"
sys.path.insert(0, str(IIKO_SCRIPTS))

from export_orders_delivery import (  # noqa: E402
    IikoClient,
    active_department_from_olap_rows,
    clean_text,
    department_scope,
    fetch_olap,
    load_local_env,
    orders_from,
    revenue_from,
    value_from,
)


START = dt.date(2026, 6, 1)
END = dt.date(2026, 6, 30)
PROCESSED_DIR = PROJECT_ROOT / "research" / "processed" / "iiko" / "orders_delivery"
JSON_PATH = PROCESSED_DIR / "site_app_revenue_2026-06.json"
CSV_PATH = PROCESSED_DIR / "site_app_revenue_2026-06.csv"


def channel_for(marketing_source: str) -> str:
    normalized = clean_text(marketing_source).casefold()
    if normalized in {"сайт", "site", "website", "web"}:
        return "website"
    if normalized in {"ios", "андройд", "андроид", "android"}:
        return "starter_app"
    return "other_or_unknown"


def main() -> int:
    load_local_env()
    manifest: list[dict[str, Any]] = []
    rows = fetch_olap(
        IikoClient(),
        manifest,
        name="site_app_revenue",
        start=START,
        end=END,
        group_rows=[
            "Department",
            "Delivery.MarketingSource",
            "OriginName",
            "Delivery.SourceKey",
        ],
        agrs=["OrderNum", "DishSumInt", "DishDiscountSumInt", "DiscountSum"],
    )

    active_department = active_department_from_olap_rows(rows)
    active_rows = [
        row
        for row in rows
        if department_scope(clean_text(value_from(row, ["Department"]))) == "active_chernikova"
    ]

    by_source: dict[str, dict[str, float]] = defaultdict(
        lambda: {"orders": 0.0, "revenue": 0.0}
    )
    by_channel: dict[str, dict[str, float]] = defaultdict(
        lambda: {"orders": 0.0, "revenue": 0.0}
    )
    for row in active_rows:
        source = clean_text(value_from(row, ["Delivery.MarketingSource"])) or "(пусто)"
        channel = channel_for(source)
        orders = orders_from(row)
        revenue = revenue_from(row)
        by_source[source]["orders"] += orders
        by_source[source]["revenue"] += revenue
        by_channel[channel]["orders"] += orders
        by_channel[channel]["revenue"] += revenue

    source_rows = [
        {
            "marketing_source": source,
            "channel": channel_for(source),
            "orders": round(metrics["orders"], 2),
            "revenue_after_discounts": round(metrics["revenue"], 2),
        }
        for source, metrics in sorted(
            by_source.items(), key=lambda item: (-item[1]["revenue"], item[0])
        )
    ]
    channel_rows = [
        {
            "channel": channel,
            "orders": round(metrics["orders"], 2),
            "revenue_after_discounts": round(metrics["revenue"], 2),
        }
        for channel, metrics in sorted(by_channel.items())
    ]

    payload = {
        "period_start": START.isoformat(),
        "period_end": END.isoformat(),
        "department": active_department.get("name", ""),
        "metric": "DishDiscountSumInt (revenue after discounts)",
        "channel_summary": channel_rows,
        "marketing_source_detail": source_rows,
        "raw_rows": len(rows),
        "active_department_rows": len(active_rows),
    }

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with CSV_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["marketing_source", "channel", "orders", "revenue_after_discounts"],
        )
        writer.writeheader()
        writer.writerows(source_rows)

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

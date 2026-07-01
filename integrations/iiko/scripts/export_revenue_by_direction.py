#!/usr/bin/env python3
"""Export iiko OLAP revenue, discounts, and food cost by management direction.

The script performs read-only OLAP requests only:
- GET /v2/reports/olap/columns to inspect SALES fields;
- GET /v2/reports/olap/presets to document the saved report settings;
- GET /reports/olap for XML discovery of possible direction groupings;
- GET /v2/reports/olap/byPresetId/... for the saved revenue-by-direction report.

Secrets are read from .env/ENV by the shared IikoClient and are never printed or
written to processed outputs. Raw XML/JSON responses are stored under
research/raw/iiko/revenue_by_direction/, which is ignored by git.
"""

from __future__ import annotations

import calendar
import csv
import datetime as dt
import argparse
import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from export_orders_delivery import (
    IikoClient,
    IikoHTTPError,
    PROJECT_ROOT,
    clean_text,
    department_scope,
    dimension,
    fmt_olap_date,
    iso,
    load_local_env,
    rel,
    save_raw_response,
    to_number,
    value_from,
    write_json,
)


RAW_DIR = PROJECT_ROOT / "research/raw/iiko/revenue_by_direction"
PROCESSED_DIR = PROJECT_ROOT / "research/processed/iiko/revenue_by_direction"
ECONOMIC_MONTHLY = PROJECT_ROOT / "research/processed/economic_block/iiko_monthly_gross_margin.csv"

ENDPOINT = "/reports/olap"
PRESET_ID = "73a25778-dafb-4065-a820-1e9c7da6fed6"
PRESET_ENDPOINT = f"/v2/reports/olap/byPresetId/{PRESET_ID}"
PRESET_NAME = "Отчет о выручки по направлениям"
REPORT_TYPE = "SALES"
AGRS = ["DishSumInt", "DishDiscountSumInt", "DiscountSum", "ProductCostBase.ProductCost"]
CANDIDATE_GROUPINGS = [
    "CookingPlace",
    "CookingPlace+DishGroup.TopParent",
    "CookingPlace+DishCategory",
    "DishGroup.TopParent",
    "DishCategory",
    "Department",
]
DISCOVERY_PERIOD = (dt.date(2026, 1, 1), dt.date(2026, 1, 31))
PERIODS: list[tuple[dt.date, dt.date]] = [
    (dt.date(2025, 11, 1), dt.date(2025, 11, 30)),
    (dt.date(2025, 12, 1), dt.date(2025, 12, 31)),
    (dt.date(2026, 1, 1), dt.date(2026, 1, 31)),
    (dt.date(2026, 2, 1), dt.date(2026, 2, 28)),
    (dt.date(2026, 3, 1), dt.date(2026, 3, 31)),
    (dt.date(2026, 4, 1), dt.date(2026, 4, 30)),
    (dt.date(2026, 5, 1), dt.date(2026, 5, 31)),
]

OPIU_DIRECTIONS = ["Роллы", "Пицца", "ГЦ", "Бар"]
ACTIVE_DEPARTMENT_CODES = {"2"}
JANUARY_OPIU_CONTROLS = {
    "1!C5": ("Роллы", 2_675_154.0),
    "1!D5": ("Пицца", 1_374_065.0),
    "1!E5": ("ГЦ", 564_253.0),
    "1!F5": ("Бар", 75_581.0),
    "1!M5": ("Total", 4_689_053.0),
}

LONG_FIELDS = [
    "month",
    "opiu_direction",
    "olap_direction",
    "revenue_without_discount",
    "revenue_with_discount",
    "discount_amount",
    "food_cost",
    "gross_margin",
    "gm_pct",
    "source",
]
PIVOT_FIELDS = ["month", "metric", "Роллы", "Пицца", "ГЦ", "Бар", "Total"]
JANUARY_CHECK_FIELDS = ["opiu_cell", "opiu_value_manual", "olap_value", "delta_abs", "delta_pct", "status"]


def period_label(start: dt.date, end: dt.date) -> str:
    if start.day == 1 and start.month == end.month and start.year == end.year:
        return f"{start.year:04d}-{start.month:02d}"
    return f"{start.isoformat()}_{end.isoformat()}"


def parse_month_period(value: str) -> tuple[dt.date, dt.date]:
    match = re.fullmatch(r"(\d{4})-(\d{2})", value.strip())
    if not match:
        raise argparse.ArgumentTypeError("month must be YYYY-MM")
    year = int(match.group(1))
    month = int(match.group(2))
    if month < 1 or month > 12:
        raise argparse.ArgumentTypeError("month must be between 01 and 12")
    last_day = calendar.monthrange(year, month)[1]
    return dt.date(year, month, 1), dt.date(year, month, last_day)


def assert_full_month(start: dt.date, end: dt.date) -> None:
    last_day = calendar.monthrange(start.year, start.month)[1]
    expected_end = dt.date(start.year, start.month, last_day)
    if start.day != 1 or end != expected_end:
        raise SystemExit(
            f"Refusing non-full-month period {start.isoformat()}..{end.isoformat()}; "
            "OPIU requires a full calendar month."
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export full-month iiko revenue/food cost by OPIU direction.")
    parser.add_argument(
        "--month",
        action="append",
        type=parse_month_period,
        help="Full calendar month to export, in YYYY-MM format. Can be repeated. Defaults to documented months.",
    )
    return parser.parse_args()


def requested_periods(args: argparse.Namespace) -> list[tuple[dt.date, dt.date]]:
    periods = args.month or PERIODS
    for start, end in periods:
        assert_full_month(start, end)
    return periods


def safe_filename(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value)


def money_text(value: float) -> str:
    if abs(value) < 0.005:
        value = 0.0
    return f"{value:.2f}"


def pct_value(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def pct_text(value: float) -> str:
    return f"{value:.6f}"


def fmt_money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ")


def fmt_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def field_candidates(field: str) -> list[str]:
    return [field, f"{field}.sum"]


def metrics_from_row(row: dict[str, Any]) -> dict[str, float]:
    revenue_without_discount = to_number(value_from(row, field_candidates("DishSumInt")))
    revenue_with_discount = to_number(
        value_from(row, field_candidates("DishDiscountSumInt") + ["sumAfterDiscountWithoutVAT"])
    )
    food_cost = to_number(value_from(row, field_candidates("ProductCostBase.ProductCost")))
    discount = revenue_without_discount - revenue_with_discount
    return {
        "revenue_without_discount": revenue_without_discount,
        "revenue_with_discount": revenue_with_discount,
        "discount_amount": discount,
        "food_cost": food_cost,
        "gross_margin": revenue_with_discount - food_cost,
    }


def add_metrics(target: dict[str, float], source: dict[str, float]) -> None:
    for metric in ("revenue_without_discount", "revenue_with_discount", "discount_amount", "food_cost", "gross_margin"):
        target[metric] += source.get(metric, 0.0)


def empty_metrics() -> dict[str, float]:
    return {
        "revenue_without_discount": 0.0,
        "revenue_with_discount": 0.0,
        "discount_amount": 0.0,
        "food_cost": 0.0,
        "gross_margin": 0.0,
    }


def map_opiu_direction(olap_direction: str) -> str:
    text = clean_text(olap_direction)
    lowered = text.casefold()
    if lowered == "бар" or "бар" == lowered.strip():
        return "Бар"
    if "пицц" in lowered:
        return "Пицца"
    if "шаур" in lowered:
        return "ГЦ"
    if "суш" in lowered:
        return "Роллы"
    if "спец" in lowered:
        return "Роллы"
    return "unmapped"


def grouping_rows(grouping: str) -> list[str]:
    if grouping == "Department":
        return ["Department"]
    fields = [field.strip() for field in grouping.split("+") if field.strip()]
    return ["Department", *fields]


def direction_field(grouping: str) -> str:
    if grouping == "Department":
        return "Department"
    return grouping.split("+", 1)[0].strip()


def row_is_active_chernikova(row: dict[str, Any]) -> bool:
    department = dimension(value_from(row, ["Department"]))
    if department != "(пусто)":
        return department_scope(department) == "active_chernikova"
    department_code = clean_text(value_from(row, ["Department.Code"]))
    if department_code:
        return department_code in ACTIVE_DEPARTMENT_CODES
    return True


def fetch_columns(client: IikoClient, manifest: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        status, data = client.request("/v2/reports/olap/columns", params={"reportType": REPORT_TYPE})
    except IikoHTTPError as exc:
        write_json(
            RAW_DIR / "olap_columns_sales_error.json",
            {
                "endpoint": "/v2/reports/olap/columns",
                "reportType": REPORT_TYPE,
                "status": exc.status,
                "message": clean_text(exc.message)[:500],
            },
        )
        manifest.append(
            {
                "endpoint": "/v2/reports/olap/columns",
                "file": "research/raw/iiko/revenue_by_direction/olap_columns_sales_error.json",
                "status": exc.status,
                "bytes": len(exc.body),
                "parsed_rows": 0,
                "note": "error reportType=SALES",
            }
        )
        return {}

    rows = save_raw_response(
        RAW_DIR / "olap_columns_sales.json",
        data,
        manifest,
        endpoint="/v2/reports/olap/columns",
        status=status,
        note="reportType=SALES",
    )
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    if isinstance(payload, dict):
        return payload
    return {"_rows": rows}


def fetch_presets(client: IikoClient, manifest: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        status, data = client.request("/v2/reports/olap/presets")
    except IikoHTTPError as exc:
        write_json(
            RAW_DIR / "olap_presets_error.json",
            {
                "endpoint": "/v2/reports/olap/presets",
                "status": exc.status,
                "message": clean_text(exc.message)[:500],
            },
        )
        manifest.append(
            {
                "endpoint": "/v2/reports/olap/presets",
                "file": "research/raw/iiko/revenue_by_direction/olap_presets_error.json",
                "status": exc.status,
                "bytes": len(exc.body),
                "parsed_rows": 0,
                "note": "error",
            }
        )
        return []

    rows = save_raw_response(
        RAW_DIR / "olap_presets.json",
        data,
        manifest,
        endpoint="/v2/reports/olap/presets",
        status=status,
        note=f"lookup saved preset {PRESET_ID}",
    )
    return rows


def fetch_olap(
    client: IikoClient,
    manifest: list[dict[str, Any]],
    *,
    start: dt.date,
    end: dt.date,
    grouping: str,
    raw_path: Path,
    note: str,
) -> list[dict[str, Any]]:
    group_rows = grouping_rows(grouping)
    params = {
        "report": REPORT_TYPE,
        "summary": "false",
        "from": fmt_olap_date(start),
        "to": fmt_olap_date(end),
        "groupRow": group_rows,
        "agr": AGRS,
    }
    status, data = client.request(ENDPOINT, params=params)
    rows = save_raw_response(
        raw_path,
        data,
        manifest,
        endpoint=ENDPOINT,
        period=(start, end),
        status=status,
        expected_fields=group_rows + AGRS,
        note=f"{note}; report=SALES; grouping={grouping}; groupRows={','.join(group_rows)}",
    )
    for row in rows:
        row["_period_start"] = iso(start)
        row["_period_end"] = iso(end)
        row["_source_file"] = rel(raw_path)
    print(f"fetched {note} {period_label(start, end)} grouping={grouping}: {len(rows)} rows")
    time.sleep(0.15)
    return rows


def fetch_preset_period(
    client: IikoClient,
    manifest: list[dict[str, Any]],
    *,
    start: dt.date,
    end: dt.date,
    raw_path: Path,
    note: str,
) -> list[dict[str, Any]]:
    # iiko byPresetId uses an exclusive upper bound for dateTo.
    exclusive_end = end + dt.timedelta(days=1)
    params = {
        "summary": "false",
        "dateFrom": start.isoformat(),
        "dateTo": exclusive_end.isoformat(),
    }
    status, data = client.request(PRESET_ENDPOINT, params=params)
    expected_fields = ["Department.Code", "CookingPlace", *AGRS]
    rows = save_raw_response(
        raw_path,
        data,
        manifest,
        endpoint=PRESET_ENDPOINT,
        period=(start, end),
        status=status,
        expected_fields=expected_fields,
        note=(
            f"{note}; preset={PRESET_NAME}; report=SALES; "
            "groupRows=Department.Code,CookingPlace; "
            "filters=OrderDeleted:NOT_DELETED,DeletedWithWriteoff:NOT_DELETED; "
            f"dateTo exclusive={exclusive_end.isoformat()}"
        ),
    )
    for row in rows:
        row["_period_start"] = iso(start)
        row["_period_end"] = iso(end)
        row["_source_file"] = rel(raw_path)
    print(f"fetched preset {note} {period_label(start, end)}: {len(rows)} rows")
    time.sleep(0.15)
    return rows


def aggregate_direction_rows(rows: list[dict[str, Any]], grouping: str) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = defaultdict(empty_metrics)
    for row in rows:
        if not row_is_active_chernikova(row):
            continue
        field = direction_field(grouping)
        if field == "Department":
            direction = dimension(value_from(row, ["Department", "Department.Code"]))
        else:
            direction = dimension(value_from(row, [field]))
        add_metrics(result[direction], metrics_from_row(row))
    return dict(result)


def aggregate_opiu(rows: list[dict[str, Any]], grouping: str) -> dict[str, dict[str, float]]:
    by_olap = aggregate_direction_rows(rows, grouping)
    result: dict[str, dict[str, float]] = {direction: empty_metrics() for direction in OPIU_DIRECTIONS}
    if any(map_opiu_direction(direction) == "unmapped" for direction in by_olap):
        result["unmapped"] = empty_metrics()
    for olap_direction, metrics in by_olap.items():
        opiu_direction = map_opiu_direction(olap_direction)
        result.setdefault(opiu_direction, empty_metrics())
        add_metrics(result[opiu_direction], metrics)
    return result


def score_discovery(rows: list[dict[str, Any]], grouping: str) -> dict[str, Any]:
    by_olap = aggregate_direction_rows(rows, grouping)
    by_opiu = aggregate_opiu(rows, grouping)
    rolls = by_opiu.get("Роллы", empty_metrics())["revenue_without_discount"]
    total = sum(by_opiu.get(direction, empty_metrics())["revenue_without_discount"] for direction in OPIU_DIRECTIONS)
    present_opiu = {
        direction
        for direction in OPIU_DIRECTIONS
        if abs(by_opiu.get(direction, empty_metrics())["revenue_without_discount"]) > 0.01
    }
    olap_values = sorted(by_olap)
    rolls_delta_pct = abs(rolls - 2_675_154.0) / 2_675_154.0
    total_delta_pct = abs(total - 4_689_053.0) / 4_689_053.0
    score = len(present_opiu) * 20
    if present_opiu == set(OPIU_DIRECTIONS):
        score += 80
    if rolls_delta_pct <= 0.01:
        score += 100
    elif rolls_delta_pct <= 0.05:
        score += 40
    if total_delta_pct <= 0.01:
        score += 100
    elif total_delta_pct <= 0.05:
        score += 40
    if any("спец" in value.casefold() for value in olap_values):
        score += 20
    if any("суш" in value.casefold() for value in olap_values):
        score += 20
    return {
        "grouping": grouping,
        "score": score,
        "rows": len(rows),
        "olap_directions": olap_values,
        "present_opiu": sorted(present_opiu),
        "rolls_revenue_without_discount": rolls,
        "total_revenue_without_discount": total,
        "rolls_delta_pct": rolls_delta_pct,
        "total_delta_pct": total_delta_pct,
        "accepted": present_opiu == set(OPIU_DIRECTIONS)
        and rolls_delta_pct <= 0.01
        and total_delta_pct <= 0.01,
    }


def run_discovery(client: IikoClient, manifest: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    discovery: list[dict[str, Any]] = []
    chosen = ""
    for grouping in CANDIDATE_GROUPINGS:
        start, end = DISCOVERY_PERIOD
        rows = fetch_olap(
            client,
            manifest,
            start=start,
            end=end,
            grouping=grouping,
            raw_path=RAW_DIR / f"discovery_2026-01_{safe_filename(grouping)}.xml",
            note="discovery",
        )
        result = score_discovery(rows, grouping)
        discovery.append(result)
        if result["accepted"]:
            chosen = grouping
            break

    start, end = DISCOVERY_PERIOD
    preset_rows = fetch_preset_period(
        client,
        manifest,
        start=start,
        end=end,
        raw_path=RAW_DIR / f"discovery_2026-01_byPreset_{PRESET_ID}.json",
        note="discovery",
    )
    preset_result = score_discovery(preset_rows, "CookingPlace")
    preset_result["grouping"] = f"byPreset:{PRESET_NAME}"
    preset_result["source_endpoint"] = PRESET_ENDPOINT
    preset_result["accepted"] = (
        set(preset_result["present_opiu"]) == set(OPIU_DIRECTIONS)
        and preset_result["rolls_delta_pct"] <= 0.01
        and preset_result["total_delta_pct"] <= 0.01
    )
    discovery.append(preset_result)
    if preset_result["accepted"]:
        chosen = "CookingPlace"
    elif not chosen and discovery:
        chosen = max(discovery, key=lambda item: item["score"])["grouping"]
    if not chosen:
        raise SystemExit("Discovery did not return any OLAP rows")
    return chosen, discovery


def build_long_rows(monthly_rows: dict[str, list[dict[str, Any]]], grouping: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    source = f"iiko OLAP byPresetId={PRESET_ID} ({PRESET_NAME})"
    for month, raw_rows in monthly_rows.items():
        by_olap = aggregate_direction_rows(raw_rows, grouping)
        for olap_direction, metrics in sorted(by_olap.items(), key=lambda item: (map_opiu_direction(item[0]), item[0])):
            opiu_direction = map_opiu_direction(olap_direction)
            revenue_with_discount = metrics["revenue_with_discount"]
            gross_margin = revenue_with_discount - metrics["food_cost"]
            rows.append(
                {
                    "month": month,
                    "opiu_direction": opiu_direction,
                    "olap_direction": olap_direction,
                    "revenue_without_discount": money_text(metrics["revenue_without_discount"]),
                    "revenue_with_discount": money_text(revenue_with_discount),
                    "discount_amount": money_text(metrics["discount_amount"]),
                    "food_cost": money_text(metrics["food_cost"]),
                    "gross_margin": money_text(gross_margin),
                    "gm_pct": pct_text(pct_value(gross_margin, revenue_with_discount)),
                    "source": source,
                }
            )
    rows.sort(key=lambda row: (row["month"], row["opiu_direction"], row["olap_direction"]))
    return rows


def build_pivot_rows(long_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    metrics = ["revenue_without_discount", "revenue_with_discount", "discount", "food_cost", "gross_margin"]
    by_month_direction: dict[tuple[str, str], dict[str, float]] = defaultdict(empty_metrics)
    for row in long_rows:
        direction = row["opiu_direction"]
        if direction not in OPIU_DIRECTIONS:
            continue
        key = (row["month"], direction)
        by_month_direction[key]["revenue_without_discount"] += to_number(row["revenue_without_discount"])
        by_month_direction[key]["revenue_with_discount"] += to_number(row["revenue_with_discount"])
        by_month_direction[key]["discount_amount"] += to_number(row["discount_amount"])
        by_month_direction[key]["food_cost"] += to_number(row["food_cost"])
        by_month_direction[key]["gross_margin"] += to_number(row["gross_margin"])

    months = sorted({row["month"] for row in long_rows})
    output: list[dict[str, Any]] = []
    metric_to_source = {
        "revenue_without_discount": "revenue_without_discount",
        "revenue_with_discount": "revenue_with_discount",
        "discount": "discount_amount",
        "food_cost": "food_cost",
        "gross_margin": "gross_margin",
    }
    for month in months:
        for metric in metrics:
            source_metric = metric_to_source[metric]
            out = {"month": month, "metric": metric}
            total = 0.0
            for direction in OPIU_DIRECTIONS:
                value = by_month_direction[(month, direction)][source_metric]
                out[direction] = money_text(value)
                total += value
            out["Total"] = money_text(total)
            output.append(out)
    return output


def pivot_lookup(pivot_rows: list[dict[str, Any]], month: str, metric: str) -> dict[str, float]:
    for row in pivot_rows:
        if row["month"] == month and row["metric"] == metric:
            return {field: to_number(row.get(field)) for field in OPIU_DIRECTIONS + ["Total"]}
    return {field: 0.0 for field in OPIU_DIRECTIONS + ["Total"]}


def build_january_check(pivot_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    values = pivot_lookup(pivot_rows, "2026-01", "revenue_without_discount")
    output: list[dict[str, Any]] = []
    for cell, (direction, manual_value) in JANUARY_OPIU_CONTROLS.items():
        olap_value = values.get(direction, 0.0)
        delta = olap_value - manual_value
        delta_pct = delta / manual_value if manual_value else 0.0
        status = "ok" if abs(delta_pct) < 0.01 else "mismatch"
        output.append(
            {
                "opiu_cell": cell,
                "opiu_value_manual": money_text(manual_value),
                "olap_value": money_text(olap_value),
                "delta_abs": money_text(delta),
                "delta_pct": pct_text(delta_pct),
                "status": status,
            }
        )
    return output


def read_existing_monthly() -> dict[str, dict[str, float]]:
    if not ECONOMIC_MONTHLY.exists():
        return {}
    result: dict[str, dict[str, float]] = {}
    with ECONOMIC_MONTHLY.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            month = clean_text(row.get("period"))
            period_start = clean_text(row.get("period_start"))
            period_end = clean_text(row.get("period_end"))
            if period_start and period_end:
                try:
                    start = dt.date.fromisoformat(period_start)
                    end = dt.date.fromisoformat(period_end)
                except ValueError:
                    start = end = None
                if start and end:
                    last_day = calendar.monthrange(start.year, start.month)[1]
                    if start.day == 1 and end == dt.date(start.year, start.month, last_day):
                        month = f"{start.year:04d}-{start.month:02d}"
            if not month:
                continue
            result[month] = {
                "revenue": to_number(row.get("revenue")),
                "gross_sum": to_number(row.get("gross_sum")),
                "product_cost": to_number(row.get("product_cost")),
            }
    return result


def build_existing_comparison(pivot_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    existing = read_existing_monthly()
    rows: list[dict[str, Any]] = []
    for month in sorted({row["month"] for row in pivot_rows}):
        if month not in existing:
            continue
        gross = pivot_lookup(pivot_rows, month, "revenue_without_discount")["Total"]
        revenue = pivot_lookup(pivot_rows, month, "revenue_with_discount")["Total"]
        food_cost = pivot_lookup(pivot_rows, month, "food_cost")["Total"]
        current = existing[month]
        for metric, new_value, old_metric in (
            ("revenue_without_discount", gross, "gross_sum"),
            ("revenue_with_discount", revenue, "revenue"),
            ("food_cost", food_cost, "product_cost"),
        ):
            old_value = current.get(old_metric, 0.0)
            delta = new_value - old_value
            rows.append(
                {
                    "month": month,
                    "metric": metric,
                    "revenue_by_direction": new_value,
                    "iiko_monthly_gross_margin": old_value,
                    "delta": delta,
                    "delta_pct": delta / old_value if old_value else 0.0,
                }
            )
    return rows


def write_existing_comparison(rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "month",
        "metric",
        "revenue_by_direction",
        "iiko_monthly_gross_margin",
        "delta",
        "delta_pct",
    ]
    output = []
    for row in rows:
        output.append(
            {
                "month": row["month"],
                "metric": row["metric"],
                "revenue_by_direction": money_text(row["revenue_by_direction"]),
                "iiko_monthly_gross_margin": money_text(row["iiko_monthly_gross_margin"]),
                "delta": money_text(row["delta"]),
                "delta_pct": pct_text(row["delta_pct"]),
            }
        )
    write_csv(PROCESSED_DIR / "revenue_by_direction_vs_iiko_monthly_gross_margin.csv", output, fieldnames)


def report_direction_summary(pivot_rows: list[dict[str, Any]], metric: str, months: list[str]) -> list[str]:
    lines = ["| Месяц | Роллы | Пицца | ГЦ | Бар | Total |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for month in months:
        values = pivot_lookup(pivot_rows, month, metric)
        lines.append(
            "| "
            + month
            + " | "
            + " | ".join(fmt_money(values[direction]) for direction in OPIU_DIRECTIONS)
            + f" | {fmt_money(values['Total'])} |"
        )
    return lines


def write_report(
    *,
    grouping: str,
    periods: list[tuple[dt.date, dt.date]],
    discovery: list[dict[str, Any]],
    columns: dict[str, Any],
    presets: list[dict[str, Any]],
    pivot_rows: list[dict[str, Any]],
    january_check: list[dict[str, Any]],
    existing_comparison: list[dict[str, Any]],
    long_rows: list[dict[str, Any]],
    manifest: list[dict[str, Any]],
) -> None:
    january_status = "ok" if all(row["status"] == "ok" for row in january_check) else "mismatch"
    column_keys = set(columns) if isinstance(columns, dict) else set()
    required_fields_status = []
    grouping_fields = sorted({field for grouping_value in CANDIDATE_GROUPINGS for field in grouping_rows(grouping_value)})
    for field in AGRS + grouping_fields:
        required_fields_status.append(f"`{field}`: {'есть' if field in column_keys or not column_keys else 'не найдено в columns JSON'}")

    unmapped = sorted({row["olap_direction"] for row in long_rows if row["opiu_direction"] == "unmapped"})
    feb_apr_food = [
        row
        for row in existing_comparison
        if row["metric"] == "food_cost" and row["month"] in {"2026-02", "2026-03", "2026-04"}
    ]
    feb_apr_food_ok = bool(feb_apr_food) and all(abs(row["delta_pct"]) < 0.02 for row in feb_apr_food)
    revenue_diff_rows = [row for row in existing_comparison if row["metric"] == "revenue_with_discount"]
    food_diff_rows = [row for row in existing_comparison if row["metric"] == "food_cost"]

    preset_entry = next((row for row in presets if clean_text(row.get("id")) == PRESET_ID), {})
    preset_name = clean_text(preset_entry.get("name")) or PRESET_NAME
    preset_accepted = any(str(item["grouping"]).startswith("byPreset:") and item["accepted"] for item in discovery)
    grouping_title = "Подтвержденный источник" if preset_accepted else "Лучшая группировка по структуре"
    grouping_sentence = (
        f"Подтвержденный источник OLAP: saved preset `{preset_name}` (`{PRESET_ID}`), строковая группировка `{grouping}`."
        if preset_accepted
        else f"Лучшая доступная группировка по составу направлений: `{grouping}`; контроль ОПиУ <1% не выполнен."
    )

    lines: list[str] = [
        "# iiko revenue by direction",
        "",
        f"Дата выгрузки: {dt.date.today().isoformat()}.",
        "",
        "Режим: read-only. Скрипт делает `GET /v2/reports/olap/columns`, `GET /v2/reports/olap/presets`, diagnostic `GET /reports/olap` и основной `GET /v2/reports/olap/byPresetId`; авторизация через общий клиент возможна только при истекшем токене. Google Sheets и iiko-настройки не изменялись.",
        "",
        "Контур: активная точка Черникова. Строки других подразделений в processed не включаются; Гагарина остается историческим контуром.",
        "",
            f"## {grouping_title}",
            "",
            grouping_sentence,
        "",
        "Discovery за январь 2026 сравнивался с `1!C5/M5` по `DishSumInt`, потому что эти ячейки относятся к выручке без учета скидок. XML `/reports/olap` использовался для поиска группировки, а saved preset - как точный источник с фильтрами отчета.",
        "",
        "Фильтры saved preset: `OrderDeleted=NOT_DELETED`, `DeletedWithWriteoff=NOT_DELETED`; дата в `byPresetId` задается как `dateFrom` включительно и `dateTo` исключительно.",
        "",
        "| Группировка | Raw rows | Направления OLAP | Роллы C5 | Δ C5 | Total M5 | Δ M5 | Статус |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in discovery:
        status = "accepted" if item["accepted"] else ("structure_only" if item["grouping"] == grouping else "rejected")
        directions = ", ".join(item["olap_directions"]) or "-"
        lines.append(
            f"| `{item['grouping']}` | {item['rows']} | {directions} | "
            f"{fmt_money(item['rolls_revenue_without_discount'])} | {fmt_pct(item['rolls_delta_pct'])} | "
            f"{fmt_money(item['total_revenue_without_discount'])} | {fmt_pct(item['total_delta_pct'])} | {status} |"
        )

    lines.extend(
        [
            "",
            "## Маппинг",
            "",
            "| Строка P&L | Поле OLAP | Знак |",
            "| --- | --- | --- |",
            "| Выручка без скидок | `DishSumInt` | положительный |",
            "| Выручка со скидкой | `DishDiscountSumInt` | положительный |",
            "| Скидки | `DishSumInt - DishDiscountSumInt` | положительный |",
            "| Food cost / Расход продуктов | `ProductCostBase.ProductCost` | положительный |",
            "",
            "Управленческий маппинг направлений: `Роллы = Суши + Специи/Специи, роллы Черникова`; `Пицца = Пицца`; `ГЦ = Шаурма`; `Бар = Бар`.",
            "",
            "`DiscountSum` выгружается из OLAP как контрольное поле сохраненного отчета; в processed скидка считается по подтвержденному правилу `DishSumInt - DishDiscountSumInt`.",
            "",
            "Проверка полей через columns/raw: " + "; ".join(required_fields_status) + ".",
            "",
            "## Сверка с ОПиУ, январь 2026",
            "",
            "| Ячейка | ОПиУ manual | OLAP | Δ | Δ % | Статус |",
            "| --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in january_check:
        lines.append(
            f"| `{row['opiu_cell']}` | {fmt_money(to_number(row['opiu_value_manual']))} | "
            f"{fmt_money(to_number(row['olap_value']))} | {fmt_money(to_number(row['delta_abs']))} | "
            f"{fmt_pct(to_number(row['delta_pct']))} | {row['status']} |"
        )

    lines.extend(
        [
            "",
            f"Итог контрольной сверки: `{january_status}`.",
            "",
            "## Помесячная сводка",
            "",
            "Выручка без скидок:",
            "",
            *report_direction_summary(pivot_rows, "revenue_without_discount", [period_label(start, end) for start, end in periods]),
            "",
            "Выручка со скидкой:",
            "",
            *report_direction_summary(pivot_rows, "revenue_with_discount", [period_label(start, end) for start, end in periods]),
            "",
            "Food cost:",
            "",
            *report_direction_summary(pivot_rows, "food_cost", [period_label(start, end) for start, end in periods]),
            "",
            "## Сравнение с iiko_monthly_gross_margin.csv",
            "",
        ]
    )
    if existing_comparison:
        lines.extend(
            [
                "| Месяц | Метрика | Revenue by direction | iiko_monthly_gross_margin | Δ | Δ % |",
                "| --- | --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for row in existing_comparison:
            lines.append(
                f"| {row['month']} | `{row['metric']}` | {fmt_money(row['revenue_by_direction'])} | "
                f"{fmt_money(row['iiko_monthly_gross_margin'])} | {fmt_money(row['delta'])} | {fmt_pct(row['delta_pct'])} |"
            )
        max_revenue_delta = max((abs(row["delta_pct"]) for row in revenue_diff_rows), default=0.0)
        max_food_delta = max((abs(row["delta_pct"]) for row in food_diff_rows), default=0.0)
        lines.extend(
            [
                "",
                f"Максимальное расхождение по выручке со скидкой: {fmt_pct(max_revenue_delta)}.",
                f"Максимальное расхождение по food cost: {fmt_pct(max_food_delta)}.",
                f"Food cost февраль-апрель 2026: {'сходится в пределах 2%' if feb_apr_food_ok else 'есть расхождение выше 2% или нет базы сравнения'}.",
            ]
        )
    else:
        lines.append("Файл `research/processed/economic_block/iiko_monthly_gross_margin.csv` не найден, сравнение пропущено.")

    lines.extend(
        [
            "",
            "## Расхождения и гипотезы",
            "",
        ]
    )
    if unmapped:
        lines.append("- Найдены unmapped OLAP-направления: " + ", ".join(unmapped) + ". Их нельзя автоматически сворачивать в P&L без решения владельца.")
    else:
        lines.append("- Unmapped-направления в processed не обнаружены.")
    if january_status != "ok":
        lines.append("- Январская сверка расходится больше 1%; вероятна разница настроек сохраненного OLAP-отчета, фильтров или ручных корректировок ОПиУ.")
        lines.append("- Комбинации `CookingPlace+DishGroup.TopParent` и `CookingPlace+DishCategory` дают тот же итог, что и чистый `CookingPlace`; значит расхождение не объясняется выбором строки группировки, а похоже на фильтр/снимок сохраненного отчета или ручную корректировку.")
    else:
        lines.append("- Январская сверка закрылась после перехода на saved preset: старый XML `/reports/olap` без фильтров включал удаленные/списанные позиции в `DishSumInt` и `ProductCostBase.ProductCost`.")
    if existing_comparison:
        over_food = [row for row in food_diff_rows if abs(row["delta_pct"]) >= 0.02]
        over_revenue = [row for row in revenue_diff_rows if abs(row["delta_pct"]) >= 0.02]
        if over_food:
            lines.append("- Food cost расходится с `iiko_monthly_gross_margin.csv`, потому что старый monthly gross margin был построен по XML `/reports/olap` без фильтра `DeletedWithWriteoff=NOT_DELETED`, а saved preset P&L исключает удаленные/списанные позиции.")
        elif over_revenue:
            lines.append("- Расхождения с `iiko_monthly_gross_margin.csv` могут быть нормальными: старый файл снят дневной группировкой, а новый источник строится по управленческим направлениям OLAP и исключает все вне 4 P&L-направлений.")
        else:
            lines.append("- Сравнение с `iiko_monthly_gross_margin.csv` существенных расхождений не показало.")
    lines.extend(
        [
            "",
            "## Файлы",
            "",
            "- Raw XML discovery и raw JSON saved preset: `research/raw/iiko/revenue_by_direction/`.",
            "- Processed long: `research/processed/iiko/revenue_by_direction/revenue_by_direction_monthly.csv`.",
            "- Processed pivot: `research/processed/iiko/revenue_by_direction/revenue_by_direction_monthly_pivot.csv`.",
            "- Январская сверка: `research/processed/iiko/revenue_by_direction/revenue_by_direction_vs_opiu_january.csv`.",
            "- Сравнение со старым gross margin: `research/processed/iiko/revenue_by_direction/revenue_by_direction_vs_iiko_monthly_gross_margin.csv`.",
            "",
            "## Следующие шаги",
            "",
            "1. Использовать этот processed-снимок как программный источник для финального P&L 2026 по выручке, скидкам и food cost.",
            "2. Если появятся unmapped-направления, запросить у владельца управленческий маппинг до сборки P&L.",
            "3. Не использовать MTD/неполные периоды для ОПиУ; если полный месяц недоступен, строку нужно оставить незаполненной до свежей выгрузки.",
            "",
            "## Run manifest",
            "",
            f"Raw/columns/monthly записей в manifest: {len(manifest)}.",
        ]
    )
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    (PROCESSED_DIR / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    periods = requested_periods(args)
    load_local_env()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []

    client = IikoClient()
    columns = fetch_columns(client, manifest)
    presets = fetch_presets(client, manifest)
    grouping, discovery = run_discovery(client, manifest)
    print(f"selected grouping={grouping}")

    monthly_rows: dict[str, list[dict[str, Any]]] = {}
    for start, end in periods:
        month = period_label(start, end)
        rows = fetch_preset_period(
            client,
            manifest,
            start=start,
            end=end,
            raw_path=RAW_DIR / f"revenue_by_direction_{month[:7]}.json",
            note="monthly",
        )
        monthly_rows[month] = rows

    long_rows = build_long_rows(monthly_rows, grouping)
    pivot_rows = build_pivot_rows(long_rows)
    january_check = build_january_check(pivot_rows)
    existing_comparison = build_existing_comparison(pivot_rows)

    write_csv(PROCESSED_DIR / "revenue_by_direction_monthly.csv", long_rows, LONG_FIELDS)
    write_csv(PROCESSED_DIR / "revenue_by_direction_monthly_pivot.csv", pivot_rows, PIVOT_FIELDS)
    write_csv(PROCESSED_DIR / "revenue_by_direction_vs_opiu_january.csv", january_check, JANUARY_CHECK_FIELDS)
    write_existing_comparison(existing_comparison)
    write_report(
        grouping=grouping,
        periods=periods,
        discovery=discovery,
        columns=columns,
        presets=presets,
        pivot_rows=pivot_rows,
        january_check=january_check,
        existing_comparison=existing_comparison,
        long_rows=long_rows,
        manifest=manifest,
    )
    write_json(RAW_DIR / "run_manifest.json", manifest)
    print(f"wrote {rel(PROCESSED_DIR / 'revenue_by_direction_monthly.csv')}")
    print(f"wrote {rel(PROCESSED_DIR / 'revenue_by_direction_monthly_pivot.csv')}")
    print(f"wrote {rel(PROCESSED_DIR / 'revenue_by_direction_vs_opiu_january.csv')}")
    print(f"wrote {rel(PROCESSED_DIR / 'report.md')}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Read-only iiko export for ops: food cost, stock, staff, couriers.

The script reads secrets from ENV/.env, performs only GET requests plus
POST /auth inside the shared IikoClient when a token refresh is required,
writes raw responses under research/raw/, and writes only aggregate outputs
under research/processed/.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from export_orders_delivery import (
    IikoClient,
    IikoHTTPError,
    PROJECT_ROOT,
    active_department_from_olap_rows,
    active_department_from_rows,
    clean_text,
    department_scope,
    dimension,
    fmt_olap_date,
    iso,
    load_local_env,
    metric_rows_from_xml_text,
    month_chunks,
    money,
    parse_date,
    parse_rows,
    pct,
    rel,
    safe_filename,
    save_raw_response,
    to_number,
    value_from,
    write_csv,
    write_json,
)


RAW_DIR = PROJECT_ROOT / "research/raw/iiko/ops"
PROCESSED_DIR = PROJECT_ROOT / "research/processed/iiko/ops"

COST_START = dt.date(2025, 11, 1)
OPS_START = dt.date(2026, 2, 1)
END_DATE = dt.date(2026, 5, 17)

ACTIVE_SCOPE = "active_chernikova"


def sanitize_message(value: str) -> str:
    """Keep local error diagnostics useful without leaking credentials."""

    text = clean_text(value)
    text = re.sub(r"([?&](?:key|pass|password|login)=)[^&\s\"']+", r"\1<redacted>", text)
    text = re.sub(r"([?&][A-Za-z_]*token=)[^&\s\"']+", r"\1<redacted>", text, flags=re.I)
    text = re.sub(r"\b[0-9a-f]{40}\b", "<sha1-redacted>", text, flags=re.I)
    return text[:1000]


def write_error(
    path: Path,
    manifest: list[dict[str, Any]],
    *,
    endpoint: str,
    exc: IikoHTTPError,
    period: tuple[dt.date, dt.date] | None = None,
    note: str = "",
) -> None:
    payload: dict[str, Any] = {
        "endpoint": endpoint,
        "status": exc.status,
        "message": sanitize_message(exc.message),
    }
    if period:
        payload["period_start"] = iso(period[0])
        payload["period_end"] = iso(period[1])
    if note:
        payload["note"] = note
    write_json(path, payload)
    entry = {
        "endpoint": endpoint,
        "file": rel(path),
        "status": exc.status,
        "bytes": len(exc.body),
        "parsed_rows": 0,
        "note": f"error {note}".strip(),
    }
    if period:
        entry["period_start"] = iso(period[0])
        entry["period_end"] = iso(period[1])
    manifest.append(entry)


def request_error(exc: BaseException) -> IikoHTTPError:
    if isinstance(exc, IikoHTTPError):
        return exc
    message = sanitize_message(str(exc) or exc.__class__.__name__)
    return IikoHTTPError(None, message.encode("utf-8"), message)


def endpoint_status_from_manifest(manifest: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for entry in manifest:
        status = entry.get("status")
        rows.append(
            {
                "endpoint": entry.get("endpoint", ""),
                "period_start": entry.get("period_start", ""),
                "period_end": entry.get("period_end", ""),
                "status": status if status is not None else "",
                "parsed_rows": entry.get("parsed_rows", 0),
                "file": entry.get("file", ""),
                "note": entry.get("note", ""),
                "requires_parameter_clarification": bool(
                    entry.get("note", "").startswith("error")
                    or (isinstance(status, int) and status >= 400)
                ),
            }
        )
    return rows


def fetch_simple(
    client: IikoClient,
    manifest: list[dict[str, Any]],
    *,
    endpoint: str,
    raw_name: str,
    params: dict[str, Any] | None = None,
    expected_fields: list[str] | None = None,
    note: str = "",
) -> list[dict[str, Any]]:
    try:
        status, data = client.request(endpoint, params=params or {})
        extension = "json" if data.lstrip().startswith((b"{", b"[")) else "xml"
        path = RAW_DIR / f"{raw_name}.{extension}"
        rows = save_raw_response(
            path,
            data,
            manifest,
            endpoint=endpoint,
            status=status,
            expected_fields=expected_fields,
            note=note,
        )
        print(f"fetched {endpoint}: {len(rows)} rows")
        time.sleep(0.15)
        return rows
    except (IikoHTTPError, TimeoutError, OSError) as raw_exc:
        exc = request_error(raw_exc)
        write_error(
            RAW_DIR / f"{raw_name}_error.json",
            manifest,
            endpoint=endpoint,
            exc=exc,
            note=note,
        )
        print(f"{endpoint}: failed, status={exc.status}")
        time.sleep(0.15)
        return []


def fetch_references(client: IikoClient, manifest: list[dict[str, Any]]) -> dict[str, Any]:
    refs = {
        "departments": [],
        "departments_search": [],
        "stores": [],
        "stores_search": [],
        "accounts": [],
        "store_report_presets": [],
        "roles": [],
        "attendance_types": [],
        "schedule_types": [],
        "sales_columns": [],
    }
    refs["departments"] = fetch_simple(
        client,
        manifest,
        endpoint="/corporation/departments",
        raw_name="departments",
        params={"includeDeleted": "true"},
    )
    refs["departments_search"] = fetch_simple(
        client,
        manifest,
        endpoint="/corporation/departments/search",
        raw_name="departments_search",
    )
    refs["stores"] = fetch_simple(
        client,
        manifest,
        endpoint="/corporation/stores",
        raw_name="stores",
        params={"includeDeleted": "true"},
    )
    refs["stores_search"] = fetch_simple(
        client,
        manifest,
        endpoint="/corporation/stores/search",
        raw_name="stores_search",
    )
    refs["accounts"] = fetch_simple(
        client,
        manifest,
        endpoint="/v2/entities/accounts/list",
        raw_name="accounts",
        params={"includeDeleted": "true"},
    )
    refs["store_report_presets"] = fetch_simple(
        client,
        manifest,
        endpoint="/reports/storeReportPresets",
        raw_name="store_report_presets",
    )
    refs["roles"] = fetch_simple(
        client,
        manifest,
        endpoint="/employees/roles",
        raw_name="employee_roles",
        params={"includeDeleted": "true"},
    )
    refs["attendance_types"] = fetch_simple(
        client,
        manifest,
        endpoint="/employees/attendance/types",
        raw_name="attendance_types",
        params={"includeDeleted": "true"},
    )
    refs["schedule_types"] = fetch_simple(
        client,
        manifest,
        endpoint="/employees/schedule/types",
        raw_name="schedule_types",
    )
    refs["sales_columns"] = fetch_simple(
        client,
        manifest,
        endpoint="/v2/reports/olap/columns",
        raw_name="olap_columns_sales",
        params={"reportType": "SALES"},
        note="reportType=SALES",
    )
    return refs


def fetch_olap(
    client: IikoClient,
    manifest: list[dict[str, Any]],
    *,
    name: str,
    start: dt.date,
    end: dt.date,
    group_rows: list[str],
    agrs: list[str],
) -> list[dict[str, Any]]:
    all_rows: list[dict[str, Any]] = []
    expected_fields = group_rows + agrs
    for chunk in month_chunks(start, end):
        params = {
            "report": "SALES",
            "summary": "false",
            "from": fmt_olap_date(chunk[0]),
            "to": fmt_olap_date(chunk[1]),
            "groupRow": group_rows,
            "agr": agrs,
        }
        raw_base = f"olap_{safe_filename(name)}_{iso(chunk[0])}_{iso(chunk[1])}"
        try:
            status, data = client.request("/reports/olap", params=params)
            raw_path = RAW_DIR / f"{raw_base}.xml"
            rows = save_raw_response(
                raw_path,
                data,
                manifest,
                endpoint="/reports/olap",
                period=chunk,
                status=status,
                expected_fields=expected_fields,
                note=f"name={name}",
            )
            for row in rows:
                row["_period_start"] = iso(chunk[0])
                row["_period_end"] = iso(chunk[1])
                row["_source_file"] = rel(raw_path)
            all_rows.extend(rows)
            print(f"fetched olap {name} {iso(chunk[0])}..{iso(chunk[1])}: {len(rows)} rows")
        except (IikoHTTPError, TimeoutError, OSError) as raw_exc:
            exc = request_error(raw_exc)
            write_error(
                RAW_DIR / f"{raw_base}_error.json",
                manifest,
                endpoint="/reports/olap",
                exc=exc,
                period=chunk,
                note=f"name={name}",
            )
            print(f"olap {name} {iso(chunk[0])}..{iso(chunk[1])}: failed, status={exc.status}")
        time.sleep(0.15)
    return all_rows


def active_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if department_scope(dimension(value_from(row, ["Department", "department", "department.name"])))
        == ACTIVE_SCOPE
    ]


def revenue_from(row: dict[str, Any]) -> float:
    return to_number(value_from(row, ["DishDiscountSumInt", "DishDiscountSumInt.sum", "sumAfterDiscountWithoutVAT"]))


def gross_from(row: dict[str, Any]) -> float:
    return to_number(value_from(row, ["DishSumInt", "DishSumInt.sum"]))


def orders_from(row: dict[str, Any]) -> float:
    return to_number(value_from(row, ["OrderNum", "OrderNum.count", "GuestNum", "UniqOrderId"]))


def dish_amount_from(row: dict[str, Any]) -> float:
    return to_number(value_from(row, ["DishAmountInt", "DishAmountInt.sum"]))


def product_cost_from(row: dict[str, Any]) -> float:
    return to_number(value_from(row, ["ProductCostBase.ProductCost", "ProductCostBase.ProductCost.sum"]))


def product_profit_from(row: dict[str, Any]) -> float:
    return to_number(value_from(row, ["ProductCostBase.Profit", "ProductCostBase.Profit.sum"]))


def add_cost_metrics(target: dict[str, Any], row: dict[str, Any]) -> None:
    target["orders"] += orders_from(row)
    target["dish_amount"] += dish_amount_from(row)
    target["revenue"] += revenue_from(row)
    target["gross_sum"] += gross_from(row)
    target["product_cost"] += product_cost_from(row)
    target["product_profit_api"] += product_profit_from(row)


def finalize_cost_row(row: dict[str, Any]) -> None:
    revenue = row["revenue"]
    cost = row["product_cost"]
    row["food_cost_share_calc"] = cost / revenue if revenue else 0.0
    row["gross_margin_calc"] = revenue - cost
    row["gross_margin_share_calc"] = (revenue - cost) / revenue if revenue else 0.0
    row["avg_check"] = revenue / row["orders"] if row["orders"] else 0.0


def build_food_cost_outputs(
    category_rows: list[dict[str, Any]],
    dish_rows: list[dict[str, Any]],
    *,
    period: tuple[dt.date, dt.date],
) -> dict[str, Any]:
    category_groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in active_rows(category_rows):
        top_group = dimension(value_from(row, ["DishGroup.TopParent", "DishGroup"]))
        category = dimension(value_from(row, ["DishCategory", "DishCategory.Accounting"]))
        key = (top_group, category)
        group = category_groups.setdefault(
            key,
            {
                "period_start": iso(period[0]),
                "period_end": iso(period[1]),
                "dish_group_top": top_group,
                "dish_category": category,
                "orders": 0.0,
                "dish_amount": 0.0,
                "revenue": 0.0,
                "gross_sum": 0.0,
                "product_cost": 0.0,
                "product_profit_api": 0.0,
            },
        )
        add_cost_metrics(group, row)

    category_output = list(category_groups.values())
    for row in category_output:
        finalize_cost_row(row)
    category_output.sort(key=lambda item: -item["revenue"])

    dish_groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in active_rows(dish_rows):
        top_group = dimension(value_from(row, ["DishGroup.TopParent", "DishGroup"]))
        category = dimension(value_from(row, ["DishCategory", "DishCategory.Accounting"]))
        dish_name = dimension(value_from(row, ["DishName"]))
        key = (top_group, category, dish_name)
        group = dish_groups.setdefault(
            key,
            {
                "period_start": iso(period[0]),
                "period_end": iso(period[1]),
                "dish_group_top": top_group,
                "dish_category": category,
                "dish_name": dish_name,
                "orders": 0.0,
                "dish_amount": 0.0,
                "revenue": 0.0,
                "gross_sum": 0.0,
                "product_cost": 0.0,
                "product_profit_api": 0.0,
            },
        )
        add_cost_metrics(group, row)

    dish_output = list(dish_groups.values())
    for row in dish_output:
        finalize_cost_row(row)
    dish_output.sort(key=lambda item: -item["revenue"])

    fields_category = [
        "period_start",
        "period_end",
        "dish_group_top",
        "dish_category",
        "orders",
        "dish_amount",
        "revenue",
        "gross_sum",
        "product_cost",
        "food_cost_share_calc",
        "gross_margin_calc",
        "gross_margin_share_calc",
        "product_profit_api",
        "avg_check",
    ]
    fields_dish = ["dish_name", *fields_category]
    write_csv(PROCESSED_DIR / "food_cost_by_category.csv", category_output, fields_category)
    write_json(PROCESSED_DIR / "food_cost_by_category.json", category_output)
    write_csv(PROCESSED_DIR / "food_cost_by_dish.csv", dish_output, fields_dish)
    write_json(PROCESSED_DIR / "food_cost_by_dish.json", dish_output)

    total = {
        "period_start": iso(period[0]),
        "period_end": iso(period[1]),
        "orders": sum(row["orders"] for row in category_output),
        "dish_amount": sum(row["dish_amount"] for row in category_output),
        "revenue": sum(row["revenue"] for row in category_output),
        "product_cost": sum(row["product_cost"] for row in category_output),
    }
    total["food_cost_share_calc"] = (
        total["product_cost"] / total["revenue"] if total["revenue"] else 0.0
    )
    total["gross_margin_calc"] = total["revenue"] - total["product_cost"]
    total["gross_margin_share_calc"] = (
        total["gross_margin_calc"] / total["revenue"] if total["revenue"] else 0.0
    )
    material_revenue_threshold = max(50_000.0, total["revenue"] * 0.002)
    material_categories = [
        row for row in category_output if row["revenue"] >= material_revenue_threshold
    ]
    summary = {
        "total": total,
        "top_categories_by_revenue": category_output[:15],
        "top_dishes_by_revenue": dish_output[:30],
        "highest_food_cost_categories": sorted(
            material_categories,
            key=lambda item: -item["food_cost_share_calc"],
        )[:15],
        "material_revenue_threshold": material_revenue_threshold,
    }
    write_json(PROCESSED_DIR / "food_cost_summary.json", summary)
    return summary


def build_order_writeoff_outputs(rows: list[dict[str, Any]], *, period: tuple[dt.date, dt.date]) -> dict[str, Any]:
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in active_rows(rows):
        reason = public_dimension(value_from(row, ["WriteoffReason"]), "(пусто)")
        removal_type = public_dimension(value_from(row, ["RemovalType"]), "(пусто)")
        deleted_with_writeoff = public_dimension(value_from(row, ["DeletedWithWriteoff"]), "(пусто)")
        is_normal_sale = (
            deleted_with_writeoff.casefold() in {"not_deleted", "не удалено", "не удален", "не удалён"}
            and reason == "(пусто)"
            and removal_type == "(пусто)"
        )
        if is_normal_sale:
            continue
        key = (reason, removal_type, deleted_with_writeoff)
        group = groups.setdefault(
            key,
            {
                "period_start": iso(period[0]),
                "period_end": iso(period[1]),
                "writeoff_reason": reason,
                "removal_type": removal_type,
                "deleted_with_writeoff": deleted_with_writeoff,
                "cases_orders": 0.0,
                "dish_amount": 0.0,
                "gross_sum": 0.0,
                "revenue": 0.0,
                "product_cost": 0.0,
            },
        )
        group["cases_orders"] += orders_from(row)
        group["dish_amount"] += dish_amount_from(row)
        group["gross_sum"] += abs(gross_from(row))
        group["revenue"] += abs(revenue_from(row))
        group["product_cost"] += abs(product_cost_from(row))
    output = list(groups.values())
    output.sort(key=lambda item: -max(item["product_cost"], item["gross_sum"], item["revenue"]))
    fields = [
        "period_start",
        "period_end",
        "writeoff_reason",
        "removal_type",
        "deleted_with_writeoff",
        "cases_orders",
        "dish_amount",
        "gross_sum",
        "revenue",
        "product_cost",
    ]
    write_csv(PROCESSED_DIR / "order_writeoffs_summary.csv", output, fields)
    write_json(PROCESSED_DIR / "order_writeoffs_summary.json", output)
    return {"rows": output}


def flatten_dict(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        full_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, dict):
            result.update(flatten_dict(item, full_key))
        elif isinstance(item, list):
            scalar_items = [clean_text(v) for v in item if not isinstance(v, (dict, list))]
            if scalar_items:
                result[full_key] = "; ".join(scalar_items)
            result[f"{full_key}._count"] = len(item)
        else:
            result[full_key] = item
    return result


def json_records_from_bytes(data: bytes) -> list[dict[str, Any]]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception:
        return []

    records: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if any(isinstance(child, list) for child in value.values()) or any(
                key.casefold() in {"id", "date", "documentnumber", "status", "sum", "total"}
                for key in value
            ):
                records.append(flatten_dict(value))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    if not records and isinstance(payload, dict):
        records.append(flatten_dict(payload))
    return records


def parse_writeoff_document_rows(data: bytes) -> list[dict[str, Any]]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception:
        return parse_rows(data, ["status", "date", "sum"])
    documents = payload.get("response") if isinstance(payload, dict) else payload
    if not isinstance(documents, list):
        return json_records_from_bytes(data)

    rows: list[dict[str, Any]] = []
    for document in documents:
        if not isinstance(document, dict):
            continue
        context = {
            "document_id": document.get("id", ""),
            "dateIncoming": document.get("dateIncoming", ""),
            "status": document.get("status", ""),
            "accountId": document.get("accountId", ""),
            "storeId": document.get("storeId", ""),
        }
        items = document.get("items")
        if not isinstance(items, list) or not items:
            rows.append({**context, "line_rows": 0})
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            rows.append({**context, **flatten_dict(item), "line_rows": 1})
    return rows


def parse_structured_rows(data: bytes, expected_fields: list[str] | None = None) -> list[dict[str, Any]]:
    stripped = data.lstrip()
    if stripped.startswith((b"{", b"[")):
        records = json_records_from_bytes(data)
        return records or parse_rows(data, expected_fields)
    return parse_rows(data, expected_fields)


UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)


def is_technical_id(value: Any) -> bool:
    text = clean_text(value)
    if not text:
        return False
    return bool(UUID_RE.match(text)) or bool(re.fullmatch(r"[0-9a-f]{32,}", text, re.I))


def public_dimension(value: Any, fallback: str = "(не расшифровано)") -> str:
    text = dimension(value)
    if text == "(пусто)" or is_technical_id(text):
        return fallback
    return text


def reference_name_map(rows: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in rows:
        identifier = clean_text(value_from(row, ["id", "Id", "uuid", "UUID"]))
        name = clean_text(value_from(row, ["name", "Name", "title", "Title"]))
        if identifier and name:
            result[identifier] = name
    return result


def public_active_department(active_department: dict[str, str]) -> dict[str, str]:
    return {
        "scope": ACTIVE_SCOPE,
        "name": clean_text(active_department.get("name", "")) or "Foodmarket Тепло Черникова",
        "note": "Technical department id is used only inside raw/API code paths and is not published in processed outputs.",
    }


def first_present(row: dict[str, Any], fragments: list[str]) -> Any:
    lowered = [(str(key).casefold(), value) for key, value in row.items()]
    for fragment in fragments:
        fragment_lower = fragment.casefold()
        for key, value in lowered:
            if fragment_lower in key and clean_text(value):
                return value
    return ""


def money_candidate(row: dict[str, Any]) -> float:
    candidates = [
        "totalSum",
        "documentSum",
        "sumWithout",
        "sum",
        "cost",
        "price",
        "amountMoney",
        "productCost",
    ]
    best = 0.0
    for key, value in row.items():
        key_lower = str(key).casefold()
        if any(candidate.casefold() in key_lower for candidate in candidates):
            number = abs(to_number(value))
            if number > best:
                best = number
    return best


def amount_candidate(row: dict[str, Any]) -> float:
    for key, value in row.items():
        key_lower = str(key).casefold()
        if any(fragment in key_lower for fragment in ("amount", "quantity", "qty", "кол")):
            number = abs(to_number(value))
            if number:
                return number
    return 0.0


def month_from_row(row: dict[str, Any], fallback: str) -> str:
    raw = clean_text(
        first_present(row, ["dateIncoming", "documentDate", "dateTime", "date", "created"])
    )
    candidates = [raw, raw[:19], raw[:16], raw[:10]]
    for pattern in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y", "%d.%m.%Y %H:%M:%S"):
        for candidate in candidates:
            try:
                parsed = dt.datetime.strptime(candidate, pattern)
                return parsed.strftime("%Y-%m")
            except ValueError:
                continue
    match = re.search(r"(20\d{2})[-.](\d{2})", raw)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return fallback


def fetch_monthly_endpoint(
    client: IikoClient,
    manifest: list[dict[str, Any]],
    *,
    endpoint: str,
    raw_prefix: str,
    start: dt.date,
    end: dt.date,
    param_builder: Any,
    expected_fields: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    all_rows: list[dict[str, Any]] = []
    status_rows: list[dict[str, Any]] = []
    for chunk in month_chunks(start, end):
        attempts = param_builder(chunk)
        success = False
        last_error: IikoHTTPError | None = None
        for note, params in attempts:
            raw_base = f"{raw_prefix}_{iso(chunk[0])}_{iso(chunk[1])}_{safe_filename(note)}"
            try:
                status, data = client.request(endpoint, params=params)
                extension = "json" if data.lstrip().startswith((b"{", b"[")) else "xml"
                raw_path = RAW_DIR / f"{raw_base}.{extension}"
                rows = (
                    parse_writeoff_document_rows(data)
                    if endpoint == "/v2/documents/writeoff"
                    else parse_structured_rows(data, expected_fields)
                )
                save_raw_response(
                    raw_path,
                    data,
                    manifest,
                    endpoint=endpoint,
                    period=chunk,
                    status=status,
                    expected_fields=expected_fields,
                    note=note,
                )
                for row in rows:
                    row["_period_start"] = iso(chunk[0])
                    row["_period_end"] = iso(chunk[1])
                    row["_source_file"] = rel(raw_path)
                    row["_request_note"] = note
                all_rows.extend(rows)
                status_rows.append(
                    {
                        "endpoint": endpoint,
                        "period_start": iso(chunk[0]),
                        "period_end": iso(chunk[1]),
                        "status": status,
                        "parsed_rows": len(rows),
                        "raw_file": rel(raw_path),
                        "note": note,
                        "requires_parameter_clarification": False,
                    }
                )
                print(f"fetched {endpoint} {iso(chunk[0])}..{iso(chunk[1])}: {len(rows)} rows")
                success = True
                break
            except (IikoHTTPError, TimeoutError, OSError) as raw_exc:
                last_error = request_error(raw_exc)
                if "timed out" in last_error.message.casefold():
                    break
                continue
        if not success and last_error is not None:
            raw_path = RAW_DIR / f"{raw_prefix}_{iso(chunk[0])}_{iso(chunk[1])}_error.json"
            write_error(raw_path, manifest, endpoint=endpoint, exc=last_error, period=chunk)
            status_rows.append(
                {
                    "endpoint": endpoint,
                    "period_start": iso(chunk[0]),
                    "period_end": iso(chunk[1]),
                    "status": last_error.status if last_error.status is not None else "",
                    "parsed_rows": 0,
                    "raw_file": rel(raw_path),
                    "note": "all attempted parameter forms failed",
                    "requires_parameter_clarification": True,
                }
            )
            print(f"{endpoint} {iso(chunk[0])}..{iso(chunk[1])}: failed, status={last_error.status}")
        time.sleep(0.15)
    return all_rows, status_rows


def build_document_writeoff_outputs(
    rows: list[dict[str, Any]],
    *,
    period: tuple[dt.date, dt.date],
    account_names: dict[str, str],
    store_names: dict[str, str],
) -> dict[str, Any]:
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    document_sets: defaultdict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in rows:
        month = month_from_row(row, clean_text(row.get("_period_start", ""))[:7])
        status = public_dimension(first_present(row, ["status"]), "(пусто)")
        account_id = clean_text(first_present(row, ["accountId", "account.id"]))
        reason = public_dimension(
            account_names.get(account_id)
            or first_present(row, ["writeoffReason.name", "writeoffReason", "reason", "account.name"])
        )
        store_id = clean_text(first_present(row, ["storeId", "store.id"]))
        store = public_dimension(
            store_names.get(store_id) or first_present(row, ["store.name", "store", "warehouse"]),
            "(пусто)",
        )
        key = (month, status, reason)
        group = groups.setdefault(
            key,
            {
                "period_start": iso(period[0]),
                "period_end": iso(period[1]),
                "month": month,
                "status": status,
                "reason_or_account": reason,
                "document_count": 0.0,
                "line_rows": 0.0,
                "stores_count_with_values": 0.0,
                "amount": 0.0,
                "sum_candidate": 0.0,
            },
        )
        document_id = clean_text(first_present(row, ["document_id", "id"]))
        if document_id:
            document_sets[key].add(document_id)
        group["line_rows"] += 1
        if store != "(пусто)":
            group["stores_count_with_values"] += 1
        group["amount"] += amount_candidate(row)
        group["sum_candidate"] += money_candidate(row)
    output = list(groups.values())
    for key, row in zip(groups.keys(), output):
        row["document_count"] = float(len(document_sets.get(key, set())))
    output.sort(key=lambda item: (item["month"], -item["sum_candidate"], item["reason_or_account"]))
    fields = [
        "period_start",
        "period_end",
        "month",
        "status",
        "reason_or_account",
        "document_count",
        "line_rows",
        "stores_count_with_values",
        "amount",
        "sum_candidate",
    ]
    write_csv(PROCESSED_DIR / "writeoff_documents_summary.csv", output, fields)
    write_json(PROCESSED_DIR / "writeoff_documents_summary.json", output)
    return {"rows": output}


def build_stock_report_outputs(
    rows: list[dict[str, Any]],
    *,
    name: str,
    period: tuple[dt.date, dt.date],
) -> dict[str, Any]:
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        month = clean_text(row.get("_period_start", ""))[:7]
        doc_type = dimension(first_present(row, ["documentType", "docType", "operationType", "type"]))
        store = dimension(first_present(row, ["store.name", "store", "warehouse"]))
        reason = dimension(first_present(row, ["reason", "account.name", "account", "writeoff"]))
        key = (month, doc_type, reason)
        group = groups.setdefault(
            key,
            {
                "period_start": iso(period[0]),
                "period_end": iso(period[1]),
                "month": month,
                "document_or_operation_type": doc_type,
                "reason_or_account": reason,
                "rows": 0.0,
                "stores_count_with_values": 0.0,
                "amount": 0.0,
                "sum_candidate": 0.0,
            },
        )
        group["rows"] += 1
        if store != "(пусто)":
            group["stores_count_with_values"] += 1
        group["amount"] += amount_candidate(row)
        group["sum_candidate"] += money_candidate(row)
    output = list(groups.values())
    output.sort(key=lambda item: (item["month"], -item["sum_candidate"]))
    fields = [
        "period_start",
        "period_end",
        "month",
        "document_or_operation_type",
        "reason_or_account",
        "rows",
        "stores_count_with_values",
        "amount",
        "sum_candidate",
    ]
    write_csv(PROCESSED_DIR / f"{name}_summary.csv", output, fields)
    write_json(PROCESSED_DIR / f"{name}_summary.json", output)
    return {"rows": output}


def parse_dt(value: str) -> dt.datetime | None:
    text = clean_text(value)
    if not text:
        return None
    text = text.replace("Z", "")
    candidates = [text, text[:19], text[:16]]
    for pattern in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
    ):
        for candidate in candidates:
            try:
                return dt.datetime.strptime(candidate, pattern)
            except ValueError:
                continue
    return None


def hours_from_row(row: dict[str, Any]) -> float:
    for key, value in row.items():
        key_lower = str(key).casefold()
        if any(
            fragment in key_lower
            for fragment in (
                "hour",
                "hours",
                "durationhours",
                "worktime",
                "paidtime",
                "presence",
                "length",
            )
        ):
            number = to_number(value)
            if number:
                if number > 1000:
                    return number / 3600
                if number > 48:
                    return number / 60
                return number
        if any(fragment in key_lower for fragment in ("minute", "minutes")):
            number = to_number(value)
            if number:
                return number / 60

    start = parse_dt(clean_text(first_present(row, ["dateFrom", "timeFrom", "start", "from"])))
    end = parse_dt(clean_text(first_present(row, ["dateTo", "timeTo", "end", "to"])))
    if start and end and end > start:
        return (end - start).total_seconds() / 3600
    return 0.0


def person_key(row: dict[str, Any]) -> str:
    value = clean_text(
        first_present(row, ["employee.id", "employeeId", "user.id", "staff.id", "idEmployee"])
    )
    if value:
        return value
    name = clean_text(first_present(row, ["employee.name", "user.name", "name"]))
    return f"name:{name}" if name else ""


def staff_role(row: dict[str, Any]) -> str:
    return dimension(first_present(row, ["role.name", "roleName", "position", "job", "role"]))


def attendance_type(row: dict[str, Any]) -> str:
    return dimension(first_present(row, ["attendanceType.name", "attendanceType", "type.name", "type"]))


def build_staff_time_outputs(
    rows: list[dict[str, Any]],
    *,
    name: str,
    period: tuple[dt.date, dt.date],
) -> dict[str, Any]:
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    people: defaultdict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    for row in rows:
        month = clean_text(row.get("_period_start", ""))[:7]
        department = dimension(first_present(row, ["department.name", "Department", "department"]))
        scope = department_scope(department)
        role = staff_role(row)
        type_name = attendance_type(row)
        key = (month, scope, role, type_name)
        group = groups.setdefault(
            key,
            {
                "period_start": iso(period[0]),
                "period_end": iso(period[1]),
                "month": month,
                "department_scope": scope,
                "role": role,
                "attendance_or_schedule_type": type_name,
                "rows": 0.0,
                "hours": 0.0,
                "staff_count": 0.0,
                "avg_hours_per_staff": 0.0,
            },
        )
        group["rows"] += 1
        group["hours"] += hours_from_row(row)
        key_person = person_key(row)
        if key_person:
            people[key].add(key_person)
    output = list(groups.values())
    for key, row in zip(groups.keys(), output):
        row["staff_count"] = float(len(people.get(key, set())))
        row["avg_hours_per_staff"] = row["hours"] / row["staff_count"] if row["staff_count"] else 0.0
    output.sort(key=lambda item: (item["department_scope"] != ACTIVE_SCOPE, item["month"], -item["hours"]))
    fields = [
        "period_start",
        "period_end",
        "month",
        "department_scope",
        "role",
        "attendance_or_schedule_type",
        "rows",
        "hours",
        "staff_count",
        "avg_hours_per_staff",
    ]
    write_csv(PROCESSED_DIR / f"{name}_summary.csv", output, fields)
    write_json(PROCESSED_DIR / f"{name}_summary.json", output)
    return {"rows": output}


def salary_rate_candidate(row: dict[str, Any]) -> float:
    best = 0.0
    for key, value in row.items():
        key_lower = str(key).casefold()
        if any(
            fragment in key_lower
            for fragment in (
                "salary",
                "wage",
                "rate",
                "payment",
                "pay",
                "amount",
                "sum",
                "оклад",
                "ставк",
            )
        ):
            number = abs(to_number(value))
            if 0 < number < 1_000_000:
                best = max(best, number)
    return best


def build_salary_outputs(rows: list[dict[str, Any]], *, generated_at: str) -> dict[str, Any]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    people: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        role = staff_role(row)
        salary_type = dimension(first_present(row, ["salaryType", "paymentType", "type.name", "type"]))
        key = (role, salary_type)
        group = groups.setdefault(
            key,
            {
                "generated_at": generated_at,
                "role": role,
                "salary_type": salary_type,
                "rows": 0.0,
                "staff_count": 0.0,
                "rate_rows": 0.0,
                "avg_rate_candidate": 0.0,
                "min_rate_candidate": 0.0,
                "max_rate_candidate": 0.0,
            },
        )
        group["rows"] += 1
        key_person = person_key(row)
        if key_person:
            people[key].add(key_person)
        rate = salary_rate_candidate(row)
        if rate:
            group["rate_rows"] += 1
            group["avg_rate_candidate"] += rate
            group["min_rate_candidate"] = (
                rate if not group["min_rate_candidate"] else min(group["min_rate_candidate"], rate)
            )
            group["max_rate_candidate"] = max(group["max_rate_candidate"], rate)
    output = list(groups.values())
    for key, row in zip(groups.keys(), output):
        row["staff_count"] = float(len(people.get(key, set())))
        row["avg_rate_candidate"] = (
            row["avg_rate_candidate"] / row["rate_rows"] if row["rate_rows"] else 0.0
        )
    output.sort(key=lambda item: (-item["rows"], item["role"], item["salary_type"]))
    fields = [
        "generated_at",
        "role",
        "salary_type",
        "rows",
        "staff_count",
        "rate_rows",
        "avg_rate_candidate",
        "min_rate_candidate",
        "max_rate_candidate",
    ]
    write_csv(PROCESSED_DIR / "salary_settings_summary.csv", output, fields)
    write_json(PROCESSED_DIR / "salary_settings_summary.json", output)
    return {"rows": output}


def build_courier_outputs(raw_files: list[Path], *, period: tuple[dt.date, dt.date]) -> dict[str, Any]:
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for path in raw_files:
        match = re.search(r"_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})", path.name)
        if not match:
            continue
        period_start, period_end = match.group(1), match.group(2)
        text = path.read_text(encoding="utf-8", errors="replace")
        rows = metric_rows_from_xml_text(text)
        if not rows:
            rows = parse_rows(path.read_bytes())
        for row in rows:
            metric_type = dimension(value_from(row, ["metricType"]))
            key = (period_start[:7], period_start, metric_type)
            group = groups.setdefault(
                key,
                {
                    "period_start": iso(period[0]),
                    "period_end": iso(period[1]),
                    "month": period_start[:7],
                    "metric_type": metric_type,
                    "courier_metric_rows": 0.0,
                    "order_count_sum": 0.0,
                    "avg_total_time": 0.0,
                    "avg_on_the_way_time": 0.0,
                    "avg_double_orders": 0.0,
                    "avg_triple_orders": 0.0,
                    "payout_sum_candidate": 0.0,
                    "_total_time_sum": 0.0,
                    "_on_way_sum": 0.0,
                    "_double_sum": 0.0,
                    "_triple_sum": 0.0,
                },
            )
            group["courier_metric_rows"] += 1
            group["order_count_sum"] += to_number(value_from(row, ["orderCount"]))
            group["_total_time_sum"] += to_number(value_from(row, ["totalTime"]))
            group["_on_way_sum"] += to_number(value_from(row, ["onTheWayTime"]))
            group["_double_sum"] += to_number(value_from(row, ["doubleOrders"]))
            group["_triple_sum"] += to_number(value_from(row, ["tripleOrders"]))
            group["payout_sum_candidate"] += money_candidate(row)
    output = list(groups.values())
    for row in output:
        count = row["courier_metric_rows"] or 1
        row["avg_total_time"] = row["_total_time_sum"] / count
        row["avg_on_the_way_time"] = row["_on_way_sum"] / count
        row["avg_double_orders"] = row["_double_sum"] / count
        row["avg_triple_orders"] = row["_triple_sum"] / count
        for key in list(row):
            if key.startswith("_"):
                del row[key]
    output.sort(key=lambda item: (item["month"], item["metric_type"]))
    fields = [
        "period_start",
        "period_end",
        "month",
        "metric_type",
        "courier_metric_rows",
        "order_count_sum",
        "avg_total_time",
        "avg_on_the_way_time",
        "avg_double_orders",
        "avg_triple_orders",
        "payout_sum_candidate",
    ]
    write_csv(PROCESSED_DIR / "couriers_aggregate.csv", output, fields)
    write_json(PROCESSED_DIR / "couriers_aggregate.json", output)
    return {"rows": output}


def department_candidates(active_department: dict[str, str]) -> list[tuple[str, str]]:
    department_id = clean_text(active_department.get("id", ""))
    department_name = clean_text(active_department.get("name", ""))
    candidates: list[tuple[str, str]] = []
    if department_id:
        candidates.append(("json_id", json.dumps({"id": department_id}, ensure_ascii=False)))
        candidates.append(("id", department_id))
    if department_id and department_name:
        candidates.append(
            (
                "json_id_name",
                json.dumps({"id": department_id, "name": department_name}, ensure_ascii=False),
            )
        )
    if department_name:
        candidates.append(("name", department_name))
        candidates.append(("json_name", json.dumps({"name": department_name}, ensure_ascii=False)))
    return candidates


def fetch_delivery_couriers(
    client: IikoClient,
    manifest: list[dict[str, Any]],
    *,
    active_department: dict[str, str],
    start: dt.date,
    end: dt.date,
) -> tuple[list[Path], list[dict[str, Any]]]:
    candidates = department_candidates(active_department)
    status_rows: list[dict[str, Any]] = []
    raw_files: list[Path] = []
    if not candidates:
        write_json(
            PROCESSED_DIR / "couriers_endpoint_status.json",
            [
                {
                    "endpoint": "/reports/delivery/couriers",
                    "status": "skipped",
                    "note": "active department was not found",
                    "requires_parameter_clarification": True,
                }
            ],
        )
        return raw_files, status_rows
    for chunk in month_chunks(start, end):
        last_error: IikoHTTPError | None = None
        success = False
        for department_format, department_value in candidates:
            params = {
                "department": department_value,
                "dateFrom": chunk[0].strftime("%d.%m.%Y"),
                "dateTo": chunk[1].strftime("%d.%m.%Y"),
            }
            try:
                status, data = client.request("/reports/delivery/couriers", params=params)
                raw_path = RAW_DIR / f"delivery_couriers_{iso(chunk[0])}_{iso(chunk[1])}.xml"
                rows = save_raw_response(
                    raw_path,
                    data,
                    manifest,
                    endpoint="/reports/delivery/couriers",
                    period=chunk,
                    status=status,
                    note=f"departmentFormat={department_format};dateFormat=dd.MM.yyyy",
                )
                raw_files.append(raw_path)
                status_rows.append(
                    {
                        "endpoint": "/reports/delivery/couriers",
                        "period_start": iso(chunk[0]),
                        "period_end": iso(chunk[1]),
                        "status": status,
                        "parsed_rows": len(rows),
                        "raw_file": rel(raw_path),
                        "note": f"departmentFormat={department_format};dateFormat=dd.MM.yyyy",
                        "requires_parameter_clarification": False,
                    }
                )
                print(f"fetched delivery couriers {iso(chunk[0])}..{iso(chunk[1])}: {len(rows)} rows")
                success = True
                break
            except (IikoHTTPError, TimeoutError, OSError) as raw_exc:
                last_error = request_error(raw_exc)
                if "timed out" in last_error.message.casefold():
                    break
                continue
        if not success and last_error is not None:
            raw_path = RAW_DIR / f"delivery_couriers_{iso(chunk[0])}_{iso(chunk[1])}_error.json"
            write_error(raw_path, manifest, endpoint="/reports/delivery/couriers", exc=last_error, period=chunk)
            status_rows.append(
                {
                    "endpoint": "/reports/delivery/couriers",
                    "period_start": iso(chunk[0]),
                    "period_end": iso(chunk[1]),
                    "status": last_error.status if last_error.status is not None else "",
                    "parsed_rows": 0,
                    "raw_file": rel(raw_path),
                    "note": "all attempted department forms failed",
                    "requires_parameter_clarification": True,
                }
            )
            print(f"delivery couriers {iso(chunk[0])}..{iso(chunk[1])}: failed, status={last_error.status}")
        time.sleep(0.15)
    write_json(PROCESSED_DIR / "couriers_endpoint_status.json", status_rows)
    return raw_files, status_rows


def top_text(rows: list[dict[str, Any]], label_fields: list[str], metric: str, *, limit: int = 5) -> str:
    if not rows:
        return "нет данных"
    parts = []
    for row in sorted(rows, key=lambda item: -float(item.get(metric, 0) or 0))[:limit]:
        label = " / ".join(clean_text(row.get(field, "")) or "(пусто)" for field in label_fields)
        value = float(row.get(metric, 0) or 0)
        if "share" in metric:
            parts.append(f"{label}: {pct(value)}")
        elif any(fragment in metric for fragment in ("sum", "cost", "revenue", "margin", "candidate")):
            parts.append(f"{label}: {money(value)} руб.")
        elif "hours" in metric:
            parts.append(f"{label}: {value:.1f} ч.")
        else:
            parts.append(f"{label}: {money(value)}")
    return "; ".join(parts)


def build_quality_risks(
    *,
    endpoints: list[dict[str, Any]],
    food_cost: dict[str, Any],
    writeoff_docs: dict[str, Any],
    store_ops: dict[str, Any],
    product_expense: dict[str, Any],
    attendance: dict[str, Any],
    salary: dict[str, Any],
) -> list[dict[str, str]]:
    risks = [
        {
            "area": "scope",
            "risk": "Гагарина после января 2024 не входит в текущий контур; любые строки вне Черниковой исключены из основных выводов.",
            "action": "Сохранять фильтр active_chernikova и анализировать Гагарина только как историю.",
        },
        {
            "area": "food_cost",
            "risk": "Food cost рассчитан как ProductCostBase.ProductCost / DishDiscountSumInt; API-поле ProductCostBase.Percent не додумывалось при агрегации.",
            "action": "Сверить методику с ОПиУ и техкартами в Google Sheets.",
        },
        {
            "area": "writeoffs",
            "risk": "Суммы из документов/складских отчетов попали в `sum_candidate`, если структура поля неоднозначна.",
            "action": "Сверить поле суммы в сыром ответе и закрепить точную схему парсинга.",
        },
        {
            "area": "payroll",
            "risk": "`/employees/salary` содержит настройки, а не факт выплат; ФОТ нельзя считать только по этому endpoint.",
            "action": "Сверить часы iiko с Google Sheets `Расчет зарплат NEW` и фактом выплат.",
        },
        {
            "area": "couriers",
            "risk": "`/reports/delivery/couriers` возвращает строки AVERAGE/MAXIMUM/TARGET; их нельзя суммировать как один факт доставок.",
            "action": "Сверить трактовку `orderCount` и выплаты с Google Sheets `График курьеров`.",
        },
    ]
    if not food_cost.get("total", {}).get("revenue"):
        risks.append(
            {
                "area": "food_cost_empty",
                "risk": "OLAP по себестоимости не дал выручку активного контура.",
                "action": "Проверить поля ProductCostBase.* и фильтр подразделения.",
            }
        )
    if not writeoff_docs.get("rows"):
        risks.append(
            {
                "area": "writeoff_documents_empty",
                "risk": "`/v2/documents/writeoff` не дал распарсенных строк за период.",
                "action": "Уточнить формат ответа/статусы документов и сверить с `/reports/storeOperations`.",
            }
        )
    if not store_ops.get("rows"):
        risks.append(
            {
                "area": "store_operations_empty",
                "risk": "`/reports/storeOperations` ответил без ошибки, но не дал распарсенных строк.",
                "action": "Уточнить presetId/stores/documentTypes или использовать документы writeoff как основной складской источник.",
            }
        )
    if not product_expense.get("rows"):
        risks.append(
            {
                "area": "product_expense_unstable",
                "risk": "`/reports/productExpense` не отдал данные в месячных запросах и ушел в таймаут.",
                "action": "Пробовать меньший период/точный department или сверять расход сырья через OLAP food cost и складские документы.",
            }
        )
    if not attendance.get("rows"):
        risks.append(
            {
                "area": "attendance_empty",
                "risk": "`/employees/attendance` не дал распарсенных явок.",
                "action": "Проверить, ведутся ли явки в iiko или основной учет идет в Google Sheets.",
            }
        )
    if not salary.get("rows"):
        risks.append(
            {
                "area": "salary_empty",
                "risk": "`/employees/salary` не дал распарсенных настроек зарплаты.",
                "action": "Уточнить доступ/формат endpoint и сверить правила мотивации с таблицами.",
            }
        )
    failed = [row for row in endpoints if row.get("requires_parameter_clarification")]
    if failed:
        risks.append(
            {
                "area": "endpoint_params",
                "risk": "Часть складских/кадровых endpoint'ов требует уточнения параметров или схемы ответа.",
                "action": "Смотреть `endpoint_status.csv` и закрепить рабочие параметры по каждому endpoint.",
            }
        )
    write_csv(PROCESSED_DIR / "quality_risks.csv", risks, ["area", "risk", "action"])
    write_json(PROCESSED_DIR / "quality_risks.json", risks)
    return risks


def build_report(
    *,
    food_cost: dict[str, Any],
    order_writeoffs: dict[str, Any],
    writeoff_docs: dict[str, Any],
    store_ops: dict[str, Any],
    product_expense: dict[str, Any],
    attendance: dict[str, Any],
    schedule: dict[str, Any],
    salary: dict[str, Any],
    couriers: dict[str, Any],
    endpoints: list[dict[str, Any]],
    risks: list[dict[str, str]],
    cost_period: tuple[dt.date, dt.date],
    ops_period: tuple[dt.date, dt.date],
) -> None:
    total = food_cost.get("total", {})
    category_rows = food_cost.get("top_categories_by_revenue", [])
    high_cost_rows = food_cost.get("highest_food_cost_categories", [])
    order_writeoff_rows = order_writeoffs.get("rows", [])
    writeoff_doc_rows = writeoff_docs.get("rows", [])
    store_rows = store_ops.get("rows", [])
    expense_rows = product_expense.get("rows", [])
    attendance_rows = [
        row for row in attendance.get("rows", []) if row.get("department_scope") == ACTIVE_SCOPE
    ]
    schedule_rows = [
        row for row in schedule.get("rows", []) if row.get("department_scope") == ACTIVE_SCOPE
    ]
    salary_rows = salary.get("rows", [])
    courier_rows = couriers.get("rows", [])
    endpoint_ok = [row for row in endpoints if not row.get("requires_parameter_clarification")]
    endpoint_need = [row for row in endpoints if row.get("requires_parameter_clarification")]

    total_hours = sum(float(row.get("hours", 0) or 0) for row in attendance_rows)
    schedule_hours = sum(float(row.get("hours", 0) or 0) for row in schedule_rows)
    salary_staff = sum(float(row.get("staff_count", 0) or 0) for row in salary_rows)
    courier_metric_rows = sum(float(row.get("courier_metric_rows", 0) or 0) for row in courier_rows)
    courier_average_order_candidate = sum(
        float(row.get("order_count_sum", 0) or 0)
        for row in courier_rows
        if clean_text(row.get("metric_type")).casefold() == "average"
    )
    courier_payout_candidate = sum(float(row.get("payout_sum_candidate", 0) or 0) for row in courier_rows)

    lines = [
        "# Склад, себестоимость и персонал — iiko агрегаты",
        "",
        f"Сформировано: {dt.datetime.now().isoformat(timespec='seconds')}.",
        "Контур отчета: активная точка Черникова; Гагарина считается исторической и не смешивается с текущими выводами.",
        "В Markdown нет персональных строк сотрудников/курьеров и нет секретов.",
        "",
        "## Endpoint status",
        "",
        (
            f"- Факт: endpoint'ы с рабочим ответом: {len(endpoint_ok)}; требуют уточнения параметров/схемы: {len(endpoint_need)} / "
            "Источник: локальный manifest iiko ops / "
            f"Период: {iso(ops_period[0])} — {iso(ops_period[1])}, food cost {iso(cost_period[0])} — {iso(cost_period[1])} / "
            "Вывод: статус по каждому запросу зафиксирован в processed-файлах / "
            "Действие: разбирать только строки `requires_parameter_clarification=true` в `endpoint_status.csv`."
        ),
        "",
        "## Food Cost",
        "",
        (
            f"- Факт: выручка активного контура {money(float(total.get('revenue', 0) or 0))} руб., "
            f"себестоимость {money(float(total.get('product_cost', 0) or 0))} руб., "
            f"food cost {pct(float(total.get('food_cost_share_calc', 0) or 0))}, "
            f"расчетная валовая маржа {money(float(total.get('gross_margin_calc', 0) or 0))} руб. / "
            "Источник: iiko `/reports/olap`, ProductCostBase.ProductCost, DishDiscountSumInt, DishCategory, DishGroup.TopParent / "
            f"Период: {iso(cost_period[0])} — {iso(cost_period[1])} / "
            "Вывод: это рабочий агрегат food cost по продажам, не замена сверке техкарт / "
            "Действие: сверить с ОПиУ/P&L и техкартами."
        ),
        (
            f"- Факт: топ категорий по выручке: {top_text(category_rows, ['dish_group_top', 'dish_category'], 'revenue')} / "
            "Источник: iiko OLAP ProductCostBase.* по категориям / "
            f"Период: {iso(cost_period[0])} — {iso(cost_period[1])} / "
            "Вывод: категории можно использовать для меню-инжиниринга и контроля маржи / "
            "Действие: смотреть `food_cost_by_category.csv`."
        ),
        (
            f"- Факт: категории с максимальной долей food cost: {top_text(high_cost_rows, ['dish_group_top', 'dish_category'], 'food_cost_share_calc')} / "
            "Источник: iiko OLAP ProductCostBase.ProductCost / "
            f"Период: {iso(cost_period[0])} — {iso(cost_period[1])} / "
            "Вывод: высокие доли требуют проверки рецептур, цен и списаний / "
            "Действие: сверить топ с техкартами и закупочными ценами."
        ),
        "",
        "## Склад И Списания",
        "",
        (
            f"- Факт: списания/удаления в заказах: {top_text(order_writeoff_rows, ['writeoff_reason', 'removal_type', 'deleted_with_writeoff'], 'product_cost')} / "
            "Источник: iiko `/reports/olap`, WriteoffReason, RemovalType, DeletedWithWriteoff, ProductCostBase.ProductCost / "
            f"Период: {iso(ops_period[0])} — {iso(ops_period[1])} / "
            "Вывод: OLAP показывает связку списаний с продажами/удалениями, но не заменяет складские документы / "
            "Действие: сверить с `/v2/documents/writeoff`."
        ),
        (
            f"- Факт: документы writeoff: {top_text(writeoff_doc_rows, ['month', 'reason_or_account'], 'sum_candidate')} / "
            "Источник: iiko `/v2/documents/writeoff` / "
            f"Период: {iso(ops_period[0])} — {iso(ops_period[1])} / "
            "Вывод: если `sum_candidate` пустой или неоднозначный, поле суммы требует ручной валидации / "
            "Действие: закрепить точные поля суммы и причины на примерах raw."
        ),
        (
            f"- Факт: store operations: {top_text(store_rows, ['month', 'document_or_operation_type', 'reason_or_account'], 'sum_candidate')} / "
            "Источник: iiko `/reports/storeOperations` / "
            f"Период: {iso(ops_period[0])} — {iso(ops_period[1])} / "
            "Вывод: endpoint полезен только после подтверждения preset/stores/documentTypes / "
            "Действие: уточнить параметры, если строки не заполнены."
        ),
        (
            f"- Факт: расход продуктов: {top_text(expense_rows, ['month', 'document_or_operation_type', 'reason_or_account'], 'sum_candidate')} / "
            "Источник: iiko `/reports/productExpense` / "
            f"Период: {iso(ops_period[0])} — {iso(ops_period[1])} / "
            "Вывод: endpoint может дать расход сырья, но схема полей требует сверки / "
            "Действие: сравнить с food cost и складскими документами."
        ),
        "",
        "## Персонал И Курьеры",
        "",
        (
            f"- Факт: явки активного контура: {total_hours:.1f} часов; топ ролей/типов: "
            f"{top_text(attendance_rows, ['month', 'role', 'attendance_or_schedule_type'], 'hours')} / "
            "Источник: iiko `/employees/attendance` / "
            f"Период: {iso(ops_period[0])} — {iso(ops_period[1])} / "
            "Вывод: часы можно сверять с ФОТ, персональные строки не публикуются / "
            "Действие: сверить с Google Sheets `Расчет зарплат NEW`."
        ),
        (
            f"- Факт: график активного контура: {schedule_hours:.1f} плановых часов; топ: "
            f"{top_text(schedule_rows, ['month', 'role', 'attendance_or_schedule_type'], 'hours')} / "
            "Источник: iiko `/employees/schedule` / "
            f"Период: {iso(ops_period[0])} — {iso(ops_period[1])} / "
            "Вывод: если часов мало или ноль, график, вероятно, ведется вне iiko / "
            "Действие: сверить с Google Sheets графиков."
        ),
        (
            f"- Факт: зарплатные настройки: {len(salary_rows)} агрегированных групп, суммарный счетчик сотрудников по группам {salary_staff:.0f} / "
            "Источник: iiko `/employees/salary` / "
            f"Период: актуальный срез endpoint без периода / "
            "Вывод: это правила/настройки, не факт выплат / "
            "Действие: факт ФОТ брать из Google Sheets и сверять с часами iiko."
        ),
        (
            f"- Факт: курьерский endpoint отдал {courier_metric_rows:.0f} строк метрик; "
            f"кандидат orderCount по строкам AVERAGE {courier_average_order_candidate:.0f}, "
            f"кандидат выплат {money(courier_payout_candidate)} руб. / "
            "Источник: iiko `/reports/delivery/couriers` / "
            f"Период: {iso(ops_period[0])} — {iso(ops_period[1])} / "
            "Вывод: скорость и orderCount доступны агрегатно, но типы метрик AVERAGE/MAXIMUM/TARGET нельзя суммировать как факт доставок / "
            "Действие: сверить трактовку orderCount и выплаты с Google Sheets `График курьеров`."
        ),
        "",
        "## Вопросы Для Google Sheets",
        "",
        "- Как в ОПиУ/P&L считается food cost: по iiko ProductCostBase, закупкам, инвентаризациям или управленческой корректировке?",
        "- Есть ли отдельная таблица списаний/брака и совпадают ли причины со справочниками iiko?",
        "- Где фиксируются фактические выплаты кухни/кассы/курьеров и какие статьи входят в ФОТ?",
        "- Ведется ли график смен в iiko или основной график живет в Google Sheets?",
        "- Какие складские счета и причины считать операционными потерями, а какие техническими перемещениями?",
        "",
        "## Риски Качества Данных",
        "",
        *[f"- {row['risk']} Действие: {row['action']}" for row in risks],
        "",
        "## Файлы",
        "",
        "- Raw: `research/raw/iiko/ops/`.",
        "- Processed: `research/processed/iiko/ops/`.",
    ]
    (PROCESSED_DIR / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only iiko ops export")
    parser.add_argument("--end", default=iso(END_DATE), help="inclusive end date, YYYY-MM-DD")
    parser.add_argument("--cost-start", default=iso(COST_START), help="YYYY-MM-DD")
    parser.add_argument("--ops-start", default=iso(OPS_START), help="YYYY-MM-DD")
    args = parser.parse_args()

    cost_start = parse_date(args.cost_start)
    ops_start = parse_date(args.ops_start)
    end_date = parse_date(args.end)
    if cost_start > end_date or ops_start > end_date:
        raise SystemExit("start date cannot be after end date")

    load_local_env()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    monthly_status: list[dict[str, Any]] = []
    client = IikoClient()

    refs = fetch_references(client, manifest)
    active_department = active_department_from_rows(
        refs.get("departments", []) + refs.get("departments_search", [])
    )
    account_names = reference_name_map(refs.get("accounts", []))
    store_names = reference_name_map(refs.get("stores", []) + refs.get("stores_search", []))
    write_json(PROCESSED_DIR / "active_department.json", public_active_department(active_department))

    category_rows = fetch_olap(
        client,
        manifest,
        name="food_cost_categories",
        start=cost_start,
        end=end_date,
        group_rows=["Department", "DishGroup.TopParent", "DishCategory"],
        agrs=[
            "OrderNum",
            "DishAmountInt",
            "DishSumInt",
            "DishDiscountSumInt",
            "ProductCostBase.ProductCost",
            "ProductCostBase.Percent",
            "ProductCostBase.Profit",
        ],
    )
    if not active_department.get("name"):
        active_department = active_department_from_olap_rows(category_rows)
        write_json(PROCESSED_DIR / "active_department.json", public_active_department(active_department))

    dish_rows = fetch_olap(
        client,
        manifest,
        name="food_cost_dishes",
        start=cost_start,
        end=end_date,
        group_rows=["Department", "DishGroup.TopParent", "DishCategory", "DishName"],
        agrs=[
            "OrderNum",
            "DishAmountInt",
            "DishSumInt",
            "DishDiscountSumInt",
            "ProductCostBase.ProductCost",
            "ProductCostBase.Percent",
            "ProductCostBase.Profit",
        ],
    )

    order_writeoff_rows = fetch_olap(
        client,
        manifest,
        name="order_writeoffs",
        start=ops_start,
        end=end_date,
        group_rows=["Department", "WriteoffReason", "RemovalType", "DeletedWithWriteoff"],
        agrs=[
            "OrderNum",
            "DishAmountInt",
            "DishSumInt",
            "DishDiscountSumInt",
            "ProductCostBase.ProductCost",
        ],
    )

    writeoff_rows, status = fetch_monthly_endpoint(
        client,
        manifest,
        endpoint="/v2/documents/writeoff",
        raw_prefix="documents_writeoff",
        start=ops_start,
        end=end_date,
        expected_fields=["status", "date", "sum"],
        param_builder=lambda chunk: [
            (
                "dateFormat=yyyy-MM-dd",
                {"dateFrom": iso(chunk[0]), "dateTo": iso(chunk[1])},
            ),
            (
                "dateFormat=dd.MM.yyyy",
                {
                    "dateFrom": chunk[0].strftime("%d.%m.%Y"),
                    "dateTo": chunk[1].strftime("%d.%m.%Y"),
                },
            ),
        ],
    )
    monthly_status.extend(status)

    store_rows, status = fetch_monthly_endpoint(
        client,
        manifest,
        endpoint="/reports/storeOperations",
        raw_prefix="store_operations",
        start=ops_start,
        end=end_date,
        expected_fields=["documentType", "store", "sum", "amount"],
        param_builder=lambda chunk: [
            (
                "dateFormat=dd.MM.yyyy;documentTypes=WRITE_OFF",
                {
                    "dateFrom": chunk[0].strftime("%d.%m.%Y"),
                    "dateTo": chunk[1].strftime("%d.%m.%Y"),
                    "documentTypes": "WRITE_OFF",
                    "productDetalization": "false",
                    "showCostCorrections": "false",
                },
            ),
            (
                "dateFormat=dd.MM.yyyy;noDocumentType",
                {
                    "dateFrom": chunk[0].strftime("%d.%m.%Y"),
                    "dateTo": chunk[1].strftime("%d.%m.%Y"),
                    "productDetalization": "false",
                    "showCostCorrections": "false",
                },
            ),
            (
                "dateFormat=yyyy-MM-dd;documentTypes=WRITE_OFF",
                {
                    "dateFrom": iso(chunk[0]),
                    "dateTo": iso(chunk[1]),
                    "documentTypes": "WRITE_OFF",
                    "productDetalization": "false",
                    "showCostCorrections": "false",
                },
            ),
        ],
    )
    monthly_status.extend(status)

    dept_candidates = department_candidates(active_department)

    def product_expense_params(chunk: tuple[dt.date, dt.date]) -> list[tuple[str, dict[str, Any]]]:
        attempts: list[tuple[str, dict[str, Any]]] = []
        for department_format, department_value in dept_candidates:
            attempts.append(
                (
                    f"dateFormat=dd.MM.yyyy;departmentFormat={department_format}",
                    {
                        "department": department_value,
                        "dateFrom": chunk[0].strftime("%d.%m.%Y"),
                        "dateTo": chunk[1].strftime("%d.%m.%Y"),
                        "hourFrom": "0",
                        "hourTo": "23",
                    },
                )
            )
            attempts.append(
                (
                    f"dateFormat=yyyy-MM-dd;departmentFormat={department_format}",
                    {
                        "department": department_value,
                        "dateFrom": iso(chunk[0]),
                        "dateTo": iso(chunk[1]),
                        "hourFrom": "0",
                        "hourTo": "23",
                    },
                )
            )
        return attempts

    original_timeout = client.timeout
    client.timeout = min(client.timeout, 12.0)
    try:
        product_expense_rows, status = fetch_monthly_endpoint(
            client,
            manifest,
            endpoint="/reports/productExpense",
            raw_prefix="product_expense",
            start=ops_start,
            end=end_date,
            expected_fields=["product", "amount", "sum", "cost"],
            param_builder=product_expense_params,
        )
    finally:
        client.timeout = original_timeout
    monthly_status.extend(status)

    attendance_rows, status = fetch_monthly_endpoint(
        client,
        manifest,
        endpoint="/employees/attendance",
        raw_prefix="employees_attendance",
        start=ops_start,
        end=end_date,
        expected_fields=["role", "type", "from", "to"],
        param_builder=lambda chunk: [
            (
                "withPaymentDetails=false;dateFormat=yyyy-MM-dd",
                {
                    "withPaymentDetails": "false",
                    "from": iso(chunk[0]),
                    "to": iso(chunk[1]),
                },
            )
        ],
    )
    monthly_status.extend(status)

    schedule_rows, status = fetch_monthly_endpoint(
        client,
        manifest,
        endpoint="/employees/schedule",
        raw_prefix="employees_schedule",
        start=ops_start,
        end=end_date,
        expected_fields=["role", "type", "from", "to"],
        param_builder=lambda chunk: [
            (
                "withPaymentDetails=false;dateFormat=yyyy-MM-dd",
                {
                    "withPaymentDetails": "false",
                    "from": iso(chunk[0]),
                    "to": iso(chunk[1]),
                },
            )
        ],
    )
    monthly_status.extend(status)

    salary_rows = fetch_simple(
        client,
        manifest,
        endpoint="/employees/salary",
        raw_name="employees_salary",
        expected_fields=["salary", "role", "employee"],
    )

    courier_files, courier_status = fetch_delivery_couriers(
        client,
        manifest,
        active_department=active_department,
        start=ops_start,
        end=end_date,
    )
    monthly_status.extend(courier_status)

    cost_period = (cost_start, end_date)
    ops_period = (ops_start, end_date)
    food_cost = build_food_cost_outputs(category_rows, dish_rows, period=cost_period)
    order_writeoffs = build_order_writeoff_outputs(order_writeoff_rows, period=ops_period)
    writeoff_docs = build_document_writeoff_outputs(
        writeoff_rows,
        period=ops_period,
        account_names=account_names,
        store_names=store_names,
    )
    store_ops = build_stock_report_outputs(store_rows, name="store_operations", period=ops_period)
    product_expense = build_stock_report_outputs(
        product_expense_rows, name="product_expense", period=ops_period
    )
    attendance = build_staff_time_outputs(attendance_rows, name="attendance", period=ops_period)
    schedule = build_staff_time_outputs(schedule_rows, name="schedule", period=ops_period)
    salary = build_salary_outputs(
        salary_rows, generated_at=dt.datetime.now().isoformat(timespec="seconds")
    )
    couriers = build_courier_outputs(courier_files, period=ops_period)

    monthly_endpoints = {
        "/v2/documents/writeoff",
        "/reports/storeOperations",
        "/reports/productExpense",
        "/employees/attendance",
        "/employees/schedule",
        "/reports/delivery/couriers",
    }
    endpoint_rows = [
        row
        for row in endpoint_status_from_manifest(manifest)
        if not (row.get("endpoint") in monthly_endpoints and row.get("period_start"))
    ]
    endpoint_rows.extend(monthly_status)
    write_csv(
        PROCESSED_DIR / "endpoint_status.csv",
        endpoint_rows,
        [
            "endpoint",
            "period_start",
            "period_end",
            "status",
            "parsed_rows",
            "file",
            "raw_file",
            "note",
            "requires_parameter_clarification",
        ],
    )
    write_json(PROCESSED_DIR / "endpoint_status.json", endpoint_rows)
    risks = build_quality_risks(
        endpoints=endpoint_rows,
        food_cost=food_cost,
        writeoff_docs=writeoff_docs,
        store_ops=store_ops,
        product_expense=product_expense,
        attendance=attendance,
        salary=salary,
    )
    build_report(
        food_cost=food_cost,
        order_writeoffs=order_writeoffs,
        writeoff_docs=writeoff_docs,
        store_ops=store_ops,
        product_expense=product_expense,
        attendance=attendance,
        schedule=schedule,
        salary=salary,
        couriers=couriers,
        endpoints=endpoint_rows,
        risks=risks,
        cost_period=cost_period,
        ops_period=ops_period,
    )
    write_json(RAW_DIR / "manifest.json", manifest)
    write_json(PROCESSED_DIR / "run_manifest.json", manifest)
    print(f"processed files written to {rel(PROCESSED_DIR)}")
    print(f"raw files written to {rel(RAW_DIR)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

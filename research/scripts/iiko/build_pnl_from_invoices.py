#!/usr/bin/env python3
"""Build P&L invoice lines from read-only iiko incoming invoices.

The script performs only GET requests against iiko:
- /v2/entities/products/list once for product names/codes;
- /documents/export/incomingInvoice month by month.

Secrets are loaded from .env/ENV by the shared IikoClient and are never printed
or written to processed outputs. Raw source responses are stored under
research/raw/, which is ignored by git. Processed outputs contain aggregates only.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import datetime as dt
import html
import json
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from export_orders_delivery import (
    IikoClient,
    IikoHTTPError,
    PROJECT_ROOT,
    clean_text,
    load_local_env,
    parse_json_rows,
    rel,
    value_from,
)


PRODUCTS_ENDPOINT = "/v2/entities/products/list"
INVOICE_ENDPOINT = "/documents/export/incomingInvoice"

RAW_DIR = PROJECT_ROOT / "research/raw/iiko/incoming_invoice"
RAW_PRODUCTS_PATH = PROJECT_ROOT / "research/raw/iiko/products_list.json"
PROCESSED_DIR = PROJECT_ROOT / "research/processed/iiko/incoming_invoice"
WHITELIST_PATH = PROJECT_ROOT / "research/processed/economic_block/pnl_product_whitelist.csv"

MONTHLY_CSV = PROCESSED_DIR / "incoming_invoice_monthly.csv"
PNL_LINE_CSV = PROCESSED_DIR / "pnl_line_monthly.csv"
REPORT_PATH = PROCESSED_DIR / "report.md"

DEFAULT_START_MONTH = "2025-11"
DEFAULT_END_MONTH = "2026-05"
TARGET_LINES = {
    "Содержание торговых точек",
    "Вспомогательные товары(учет по факту оплаты)",
}

MONTHLY_FIELDS = [
    "month",
    "pnl_line",
    "component",
    "product_name",
    "sum_total",
    "item_count",
    "source",
]
PNL_FIELDS = ["month", "pnl_line", "total_sum", "components_breakdown_json"]


@dataclass(frozen=True)
class WhitelistEntry:
    pnl_line: str
    component: str
    excel_group: str
    excel_row: str
    include_status: str
    product_id: str
    product_code: str
    product_name: str
    march_api_sum: Decimal
    march_excel_group_sum: Decimal | None
    control_status: str


@dataclass(frozen=True)
class InvoiceItem:
    month: str
    source: str
    document_number: str
    document_date: str
    status: str
    product_id: str
    product_name: str
    product_code: str
    amount: Decimal
    item_sum: Decimal


def month_start(value: str) -> dt.date:
    return dt.datetime.strptime(value, "%Y-%m").date()


def month_end(value: dt.date) -> dt.date:
    return dt.date(value.year, value.month, calendar.monthrange(value.year, value.month)[1])


def next_month(value: dt.date) -> dt.date:
    if value.month == 12:
        return dt.date(value.year + 1, 1, 1)
    return dt.date(value.year, value.month + 1, 1)


def month_key(value: dt.date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def month_periods(start_month: str, end_month: str) -> list[tuple[str, dt.date, dt.date]]:
    start = month_start(start_month)
    end = month_start(end_month)
    if start > end:
        raise SystemExit(f"start month {start_month} is after end month {end_month}")

    result: list[tuple[str, dt.date, dt.date]] = []
    current = start
    while current <= end:
        result.append((month_key(current), current, month_end(current)))
        current = next_month(current)
    return result


def decimal_value(value: Any) -> Decimal:
    text = clean_text(value).replace(" ", "").replace("\u2212", "-")
    if not text:
        return Decimal("0")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")


def nullable_decimal(value: Any) -> Decimal | None:
    text = clean_text(value)
    if not text:
        return None
    return decimal_value(text)


def money_text(value: Decimal) -> str:
    if abs(value) < Decimal("0.005"):
        value = Decimal("0")
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def money_human(value: Decimal) -> str:
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{rounded:,.2f}".replace(",", " ")


def normalize_name(value: Any) -> str:
    return re.sub(r"\s+", " ", clean_text(value)).strip().casefold()


def parse_payload(data: bytes) -> Any:
    stripped = data.lstrip()
    if stripped.startswith((b"{", b"[")):
        return json.loads(data.decode("utf-8"))
    return xml_payload(data)


def xml_payload(data: bytes) -> dict[str, Any]:
    text = data.decode("utf-8", "replace")
    documents: list[dict[str, Any]] = []

    for body in xml_blocks(text, ["document"]):
        document = document_from_xml_body(body)
        if document.get("item"):
            documents.append(document)

    if not documents:
        for body in xml_blocks(text, ["incomingInvoice"]):
            document = document_from_xml_body(body)
            if document.get("item"):
                documents.append(document)

    if documents:
        return {"document": documents, "_source_format": "xml"}
    return {"_source_format": "xml", "_raw_text": text}


def xml_blocks(text: str, tag_names: list[str]) -> list[str]:
    names = "|".join(re.escape(name) for name in tag_names)
    pattern = re.compile(
        rf"<(?P<tag>(?:[\w.-]+:)?(?:{names}))\b[^>]*>(?P<body>.*?)</(?P=tag)>",
        re.IGNORECASE | re.DOTALL,
    )
    return [match.group("body") for match in pattern.finditer(text)]


def document_from_xml_body(body: str) -> dict[str, Any]:
    item_pattern = xml_block_pattern(["item"])
    items = [xml_simple_fields(match.group("body")) for match in item_pattern.finditer(body)]
    body_without_items = item_pattern.sub("", body)
    nested_documents = xml_blocks(body_without_items, ["document"])
    field_body = nested_documents[0] if nested_documents else body_without_items
    document = xml_simple_fields(field_body)
    document["item"] = items
    return document


def xml_block_pattern(tag_names: list[str]) -> re.Pattern[str]:
    names = "|".join(re.escape(name) for name in tag_names)
    return re.compile(
        rf"<(?P<tag>(?:[\w.-]+:)?(?:{names}))\b[^>]*>(?P<body>.*?)</(?P=tag)>",
        re.IGNORECASE | re.DOTALL,
    )


def xml_simple_fields(body: str) -> dict[str, str]:
    field_pattern = re.compile(
        r"<(?P<tag>(?:[\w.-]+:)?[\w.-]+)\b[^>]*>(?P<value>.*?)</(?P=tag)>"
        r"|<(?P<empty>(?:[\w.-]+:)?[\w.-]+)\b[^>]*/>",
        re.DOTALL,
    )
    result: dict[str, str] = {}
    for match in field_pattern.finditer(body):
        raw_tag = match.group("tag") or match.group("empty")
        if not raw_tag:
            continue
        tag = raw_tag.rsplit(":", 1)[-1]
        if tag.casefold() in {"item", "items", "document"}:
            continue
        raw_value = match.group("value")
        if raw_value is None:
            result[tag] = ""
            continue
        if "<" in raw_value:
            continue
        result[tag] = clean_text(html.unescape(raw_value))
    return result


def write_raw_json(path: Path, data: bytes) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = parse_payload(data)
    if data.lstrip().startswith((b"{", b"[")):
        path.write_bytes(data)
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def read_whitelist() -> list[WhitelistEntry]:
    with WHITELIST_PATH.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    entries: list[WhitelistEntry] = []
    for row in rows:
        pnl_line = clean_text(row.get("pnl_line"))
        source_endpoint = clean_text(row.get("source_endpoint"))
        if pnl_line not in TARGET_LINES or source_endpoint != INVOICE_ENDPOINT:
            continue
        entries.append(
            WhitelistEntry(
                pnl_line=pnl_line,
                component=clean_text(row.get("component")),
                excel_group=clean_text(row.get("excel_group")),
                excel_row=clean_text(row.get("excel_row")),
                include_status=clean_text(row.get("include_status")).casefold(),
                product_id=clean_text(row.get("iiko_product_id")).casefold(),
                product_code=clean_text(row.get("iiko_product_code")),
                product_name=clean_text(row.get("iiko_product_name")),
                march_api_sum=decimal_value(row.get("march_2026_api_sum")),
                march_excel_group_sum=nullable_decimal(row.get("march_2026_excel_group_sum")),
                control_status=clean_text(row.get("control_status")),
            )
        )
    return entries


def rows_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "data", "rows", "result", "products"):
            value = payload.get(key)
            if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                return value
        return parse_json_rows(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return []


def product_id_from(row: dict[str, Any]) -> str:
    return clean_text(value_from(row, ["id", "productId", "product_id", "uuid", "guid"])).casefold()


def product_code_from(row: dict[str, Any]) -> str:
    return clean_text(value_from(row, ["code", "num", "number", "sku", "article"]))


def product_name_from(row: dict[str, Any]) -> str:
    return clean_text(value_from(row, ["name", "productName", "fullName"]))


def product_catalog(payload: Any) -> dict[str, dict[str, str]]:
    catalog: dict[str, dict[str, str]] = {}
    for row in rows_from_payload(payload):
        product_id = product_id_from(row)
        product_name = product_name_from(row)
        if not product_id or not product_name:
            continue
        catalog[product_id] = {
            "id": product_id,
            "name": product_name,
            "code": product_code_from(row),
        }
    return catalog


def fetch_products(client: IikoClient) -> tuple[Any, dict[str, dict[str, str]]]:
    status, data = client.request(PRODUCTS_ENDPOINT, params={"includeDeleted": "true"})
    payload = write_raw_json(RAW_PRODUCTS_PATH, data)
    catalog = product_catalog(payload)
    if not catalog:
        raise RuntimeError(f"{PRODUCTS_ENDPOINT} returned no parseable product rows, status={status}")
    print(f"products: {len(catalog)}")
    time.sleep(0.15)
    return payload, catalog


def dict_values(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def looks_like_invoice_item(row: dict[str, Any]) -> bool:
    product = clean_text(value_from(row, ["product", "productId", "product_id", "product.id"]))
    item_sum = clean_text(value_from(row, ["sum", "total", "amountSum"]))
    return bool(product and item_sum)


def item_rows_from_document(doc: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("item", "items", "Item", "Items"):
        rows = dict_values(doc.get(key))
        if rows and any(looks_like_invoice_item(row) for row in rows):
            return rows
    nested = doc.get("document")
    if isinstance(nested, dict):
        return item_rows_from_document(nested)
    return []


def status_from_document(doc: dict[str, Any]) -> str:
    nested = doc.get("document")
    if isinstance(nested, dict):
        nested_status = status_from_document(nested)
        if nested_status:
            return nested_status
    return clean_text(value_from(doc, ["status", "document.status", "Document.Status"]))


def doc_field(doc: dict[str, Any], candidates: list[str]) -> str:
    nested = doc.get("document")
    if isinstance(nested, dict):
        value = doc_field(nested, candidates)
        if value:
            return value
    return clean_text(value_from(doc, candidates))


def find_invoice_documents(payload: Any) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    seen: set[int] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            ident = id(value)
            if ident not in seen and item_rows_from_document(value):
                documents.append(value)
                seen.add(ident)
                return
            for nested_key in ("document", "documents", "incomingInvoice", "incomingInvoices", "data", "result", "items"):
                if nested_key in value:
                    walk(value[nested_key])
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return documents


def fetch_invoices(
    client: IikoClient,
    *,
    month: str,
    start: dt.date,
    end: dt.date,
    products: dict[str, dict[str, str]],
) -> list[InvoiceItem]:
    status, data = client.request(
        INVOICE_ENDPOINT,
        params={"from": start.isoformat(), "to": end.isoformat()},
    )
    raw_path = RAW_DIR / f"{month}.json"
    payload = write_raw_json(raw_path, data)
    documents = find_invoice_documents(payload)
    items: list[InvoiceItem] = []
    for doc in documents:
        doc_status = status_from_document(doc)
        if doc_status != "PROCESSED":
            continue
        document_number = doc_field(doc, ["documentNumber", "number", "num"])
        document_date = doc_field(doc, ["dateIncoming", "incomingDate", "date"])
        for item in item_rows_from_document(doc):
            product_id = clean_text(
                value_from(item, ["product", "productId", "product_id", "product.id"])
            ).casefold()
            product = products.get(product_id, {})
            product_name = product.get("name") or clean_text(
                value_from(item, ["productName", "name", "product.name"])
            )
            product_code = product.get("code") or clean_text(
                value_from(item, ["productCode", "code", "product.code"])
            )
            items.append(
                InvoiceItem(
                    month=month,
                    source=rel(raw_path),
                    document_number=document_number,
                    document_date=document_date,
                    status=doc_status,
                    product_id=product_id,
                    product_name=product_name,
                    product_code=product_code,
                    amount=decimal_value(value_from(item, ["amount", "quantity", "qty"])),
                    item_sum=decimal_value(value_from(item, ["sum", "total", "amountSum"])),
                )
            )
    print(f"{month}: status={status}, processed_items={len(items)}")
    time.sleep(0.15)
    return items


def whitelist_indexes(
    entries: list[WhitelistEntry],
) -> tuple[dict[str, list[WhitelistEntry]], dict[str, list[WhitelistEntry]]]:
    by_id: dict[str, list[WhitelistEntry]] = defaultdict(list)
    by_name: dict[str, list[WhitelistEntry]] = defaultdict(list)
    for entry in entries:
        if entry.product_id:
            by_id[entry.product_id].append(entry)
        by_name[normalize_name(entry.product_name)].append(entry)
    return by_id, by_name


def resolve_entry(
    item: InvoiceItem,
    by_id: dict[str, list[WhitelistEntry]],
    by_name: dict[str, list[WhitelistEntry]],
) -> tuple[WhitelistEntry | None, str]:
    if item.product_id and item.product_id in by_id:
        entries = by_id[item.product_id]
        if len(entries) == 1:
            return entries[0], "guid"
        include_entries = [entry for entry in entries if entry.include_status == "include"]
        if len(include_entries) == 1:
            return include_entries[0], "guid"
        return None, "ambiguous_guid"

    name = normalize_name(item.product_name)
    if name and name in by_name:
        entries = by_name[name]
        if len(entries) == 1:
            return entries[0], "name"
        include_entries = [entry for entry in entries if entry.include_status == "include"]
        if len(include_entries) == 1:
            return include_entries[0], "name"
        return None, "ambiguous_name"
    return None, "not_in_whitelist"


def aggregate_items(
    items: list[InvoiceItem],
    entries: list[WhitelistEntry],
) -> dict[str, Any]:
    by_id, by_name = whitelist_indexes(entries)
    detail: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    line_totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    component_totals: dict[tuple[str, str, str], dict[str, Any]] = {}
    skipped_review: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    excluded_hits: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    match_issues: list[dict[str, Any]] = []
    match_modes: dict[str, int] = defaultdict(int)

    for item in items:
        entry, match_mode = resolve_entry(item, by_id, by_name)
        match_modes[match_mode] += 1
        if entry is None:
            if match_mode.startswith("ambiguous"):
                match_issues.append(
                    {
                        "month": item.month,
                        "product_id": item.product_id,
                        "product_name": item.product_name,
                        "sum": money_text(item.item_sum),
                        "issue": match_mode,
                    }
                )
            continue

        if entry.include_status == "include":
            product_name = item.product_name or entry.product_name
            key = (item.month, entry.pnl_line, entry.component, product_name)
            if key not in detail:
                detail[key] = {
                    "month": item.month,
                    "pnl_line": entry.pnl_line,
                    "component": entry.component,
                    "product_name": product_name,
                    "sum_total": Decimal("0"),
                    "item_count": 0,
                    "source": item.source,
                }
            detail[key]["sum_total"] += item.item_sum
            detail[key]["item_count"] += 1
            line_totals[(item.month, entry.pnl_line)] += item.item_sum

            component_key = (item.month, entry.pnl_line, entry.component)
            if component_key not in component_totals:
                component_totals[component_key] = {"sum_total": Decimal("0"), "item_count": 0}
            component_totals[component_key]["sum_total"] += item.item_sum
            component_totals[component_key]["item_count"] += 1
        elif entry.include_status == "requires_owner_review":
            key = (item.month, entry.pnl_line, entry.component, entry.excel_group, item.product_name or entry.product_name)
            if key not in skipped_review:
                skipped_review[key] = {"sum_total": Decimal("0"), "item_count": 0}
            skipped_review[key]["sum_total"] += item.item_sum
            skipped_review[key]["item_count"] += 1
        else:
            key = (item.month, entry.pnl_line, entry.component, entry.excel_group, item.product_name or entry.product_name)
            if key not in excluded_hits:
                excluded_hits[key] = {"sum_total": Decimal("0"), "item_count": 0}
            excluded_hits[key]["sum_total"] += item.item_sum
            excluded_hits[key]["item_count"] += 1

    monthly_rows = []
    for key in sorted(detail):
        row = detail[key]
        monthly_rows.append(
            {
                "month": row["month"],
                "pnl_line": row["pnl_line"],
                "component": row["component"],
                "product_name": row["product_name"],
                "sum_total": money_text(row["sum_total"]),
                "item_count": row["item_count"],
                "source": row["source"],
            }
        )

    pnl_rows = []
    for month, pnl_line in sorted(line_totals):
        breakdown: dict[str, Any] = {}
        for (component_month, component_line, component), data in sorted(component_totals.items()):
            if component_month == month and component_line == pnl_line:
                breakdown[component] = {
                    "sum_total": money_text(data["sum_total"]),
                    "item_count": data["item_count"],
                }
        pnl_rows.append(
            {
                "month": month,
                "pnl_line": pnl_line,
                "total_sum": money_text(line_totals[(month, pnl_line)]),
                "components_breakdown_json": json.dumps(breakdown, ensure_ascii=False, sort_keys=True),
            }
        )

    return {
        "monthly_rows": monthly_rows,
        "pnl_rows": pnl_rows,
        "skipped_review": skipped_review,
        "excluded_hits": excluded_hits,
        "match_issues": match_issues,
        "match_modes": match_modes,
    }


def month_line_map(pnl_rows: list[dict[str, Any]]) -> dict[tuple[str, str], Decimal]:
    return {
        (row["month"], row["pnl_line"]): decimal_value(row["total_sum"])
        for row in pnl_rows
    }


def march_control(entries: list[WhitelistEntry], monthly_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actual_by_group: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for row in monthly_rows:
        if row["month"] != "2026-03":
            continue
        matching = [
            entry
            for entry in entries
            if entry.include_status == "include"
            and entry.pnl_line == row["pnl_line"]
            and entry.component == row["component"]
            and normalize_name(entry.product_name) == normalize_name(row["product_name"])
        ]
        if not matching:
            continue
        actual_by_group[(row["pnl_line"], matching[0].excel_group)] += decimal_value(row["sum_total"])

    expected_by_group: dict[tuple[str, str], Decimal] = {}
    rows_by_group: dict[tuple[str, str], set[str]] = defaultdict(set)
    for entry in entries:
        if entry.include_status != "include" or entry.march_excel_group_sum is None:
            continue
        key = (entry.pnl_line, entry.excel_group)
        expected_by_group[key] = entry.march_excel_group_sum
        rows_by_group[key].add(entry.excel_row)

    controls: list[dict[str, Any]] = []
    for key in sorted(expected_by_group):
        actual = actual_by_group.get(key, Decimal("0"))
        expected = expected_by_group[key]
        delta = actual - expected
        controls.append(
            {
                "pnl_line": key[0],
                "excel_group": key[1],
                "excel_rows": ", ".join(sorted(rows_by_group[key])),
                "actual": actual,
                "expected": expected,
                "delta": delta,
                "status": "ok" if abs(delta) <= Decimal("0.01") else "mismatch",
            }
        )
    return controls


def format_markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["_Нет строк._"]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return lines


def write_report(
    *,
    periods: list[tuple[str, dt.date, dt.date]],
    entries: list[WhitelistEntry],
    products_count: int,
    invoice_items: list[InvoiceItem],
    aggregate: dict[str, Any],
) -> None:
    pnl_rows = aggregate["pnl_rows"]
    monthly_rows = aggregate["monthly_rows"]
    totals = month_line_map(pnl_rows)
    controls = march_control(entries, monthly_rows)
    review_hits: dict[tuple[str, str, str, str, str], dict[str, Any]] = aggregate["skipped_review"]
    excluded_hits: dict[tuple[str, str, str, str, str], dict[str, Any]] = aggregate["excluded_hits"]
    match_issues: list[dict[str, Any]] = aggregate["match_issues"]
    match_modes: dict[str, int] = aggregate["match_modes"]

    lines: list[str] = [
        "# iiko incomingInvoice -> P&L lines",
        "",
        f"Дата сборки: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}.",
        "",
        "Источник: `GET /resto/api/documents/export/incomingInvoice`, только `status=PROCESSED`; суммы из `item.sum`.",
        f"Whitelist: `{rel(WHITELIST_PATH)}`, только `include_status=include` попадает в основную сумму.",
        f"Справочник товаров: `{rel(RAW_PRODUCTS_PATH)}`, товаров распознано: {products_count}.",
        f"Товарных строк из проведенных накладных распознано: {len(invoice_items)}.",
        "",
        "## Периоды",
        "",
    ]
    period_rows = [
        [label, start.isoformat(), end.isoformat(), f"`{rel(RAW_DIR / f'{label}.json')}`"]
        for label, start, end in periods
    ]
    lines.extend(format_markdown_table(["month", "from", "to", "raw"], period_rows))
    lines.extend(["", "## Суммы по строкам ОПиУ", ""])

    line_rows: list[list[str]] = []
    for label, _, _ in periods:
        for pnl_line in sorted(TARGET_LINES):
            line_rows.append([label, pnl_line, money_human(totals.get((label, pnl_line), Decimal("0")))])
    lines.extend(format_markdown_table(["month", "pnl_line", "total_sum"], line_rows))

    lines.extend(["", "## Март 2026: контроль Excel", ""])
    control_rows = [
        [
            row["pnl_line"],
            row["excel_rows"],
            row["excel_group"],
            money_human(row["actual"]),
            money_human(row["expected"]),
            money_human(row["delta"]),
            row["status"],
        ]
        for row in controls
    ]
    lines.extend(
        format_markdown_table(
            ["pnl_line", "rows", "component", "api_sum", "excel_sum", "delta", "status"],
            control_rows,
        )
    )

    mismatches = [row for row in controls if row["status"] != "ok"]
    lines.extend(["", "## Расхождения", ""])
    if mismatches:
        mismatch_rows = [
            [
                row["pnl_line"],
                row["excel_group"],
                money_human(row["actual"]),
                money_human(row["expected"]),
                money_human(row["delta"]),
            ]
            for row in mismatches
        ]
        lines.extend(format_markdown_table(["pnl_line", "component", "api_sum", "excel_sum", "delta"], mismatch_rows))
    else:
        lines.append("Мартовские контрольные группы сошлись с Excel в пределах 0.01 руб.")

    lines.extend(["", "## requires_owner_review не включено", ""])
    if review_hits:
        review_rows = [
            [
                key[0],
                key[1],
                key[2],
                key[3],
                key[4],
                money_human(value["sum_total"]),
                str(value["item_count"]),
            ]
            for key, value in sorted(review_hits.items())
        ]
        lines.extend(
            format_markdown_table(
                ["month", "pnl_line", "component", "excel_group", "product", "sum", "item_count"],
                review_rows,
            )
        )
    else:
        lines.append("В targeted whitelist для `incomingInvoice` нет фактических попаданий со статусом `requires_owner_review`.")

    lines.extend(["", "## exclude не включено", ""])
    if excluded_hits:
        excluded_rows = [
            [
                key[0],
                key[1],
                key[2],
                key[3],
                key[4],
                money_human(value["sum_total"]),
                str(value["item_count"]),
            ]
            for key, value in sorted(excluded_hits.items())
        ]
        lines.extend(
            format_markdown_table(
                ["month", "pnl_line", "component", "excel_group", "product", "sum", "item_count"],
                excluded_rows,
            )
        )
    else:
        lines.append("Фактических попаданий по `exclude` в targeted whitelist не найдено.")

    lines.extend(["", "## Качество соединения", ""])
    mode_rows = [[mode, str(count)] for mode, count in sorted(match_modes.items())]
    lines.extend(format_markdown_table(["match_mode", "item_count"], mode_rows))
    lines.append("")
    if match_issues:
        issue_rows = [
            [row["month"], row["product_id"], row["product_name"], row["sum"], row["issue"]]
            for row in match_issues[:100]
        ]
        lines.extend(format_markdown_table(["month", "product_id", "product_name", "sum", "issue"], issue_rows))
    else:
        lines.append("Неоднозначных совпадений whitelist не найдено.")

    lines.extend(
        [
            "",
            "## Выходные файлы",
            "",
            f"- `{rel(MONTHLY_CSV)}`",
            f"- `{rel(PNL_LINE_CSV)}`",
            f"- `{rel(REPORT_PATH)}`",
            "",
            "## Примечание о диапазоне",
            "",
            f"Диапазон `{periods[0][0]}..{periods[-1][0]}` включает {len(periods)} месяцев.",
        ]
    )
    today = dt.date.today()
    if periods[-1][2] > today:
        lines.append(
            f"Последний период запрошен до `{periods[-1][2].isoformat()}`; "
            f"на дату запуска `{today.isoformat()}` это предварительный месяц."
        )
    if periods[0][0] == DEFAULT_START_MONTH and periods[-1][0] == DEFAULT_END_MONTH:
        lines.append("Если нужен ровно шестимесячный full-month срез, запускать с `--end-month 2026-04`.")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-month", default=DEFAULT_START_MONTH, help="YYYY-MM, default: 2025-11")
    parser.add_argument("--end-month", default=DEFAULT_END_MONTH, help="YYYY-MM, default: 2026-05")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    periods = month_periods(args.start_month, args.end_month)

    load_local_env()
    entries = read_whitelist()
    if not entries:
        raise SystemExit(f"No targeted whitelist entries found in {WHITELIST_PATH}")

    client = IikoClient()
    _, products = fetch_products(client)

    invoice_items: list[InvoiceItem] = []
    for label, start, end in periods:
        invoice_items.extend(
            fetch_invoices(client, month=label, start=start, end=end, products=products)
        )

    aggregate = aggregate_items(invoice_items, entries)
    write_csv(MONTHLY_CSV, aggregate["monthly_rows"], MONTHLY_FIELDS)
    write_csv(PNL_LINE_CSV, aggregate["pnl_rows"], PNL_FIELDS)
    write_report(
        periods=periods,
        entries=entries,
        products_count=len(products),
        invoice_items=invoice_items,
        aggregate=aggregate,
    )

    print(f"wrote {rel(MONTHLY_CSV)}")
    print(f"wrote {rel(PNL_LINE_CSV)}")
    print(f"wrote {rel(REPORT_PATH)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except IikoHTTPError as exc:
        message = clean_text(exc.message)
        message = re.sub(r"([?&](?:key|pass|password|login)=)[^&\s\"']+", r"\1<redacted>", message)
        message = re.sub(r"\b[0-9a-f]{40}\b", "<sha1-redacted>", message, flags=re.I)
        print(f"iiko request failed: status={exc.status}, message={message[:500]}", file=sys.stderr)
        raise SystemExit(1)

#!/usr/bin/env python3
"""Build P&L inventory result lines from read-only iiko inventory documents.

The script performs only GET requests against iiko:
- /reports/storeOperations month by month for INCOMING_INVENTORY documents;
- /v2/entities/products/list only when the local products snapshot is missing
  or --refresh-products is passed.

Secrets are loaded from .env/ENV by the shared IikoClient and are never printed
or written to processed outputs. Raw normalized source rows are stored under
research/raw/iiko/inventory/.
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


INVENTORY_ENDPOINT = "/reports/storeOperations"
PRODUCTS_ENDPOINT = "/v2/entities/products/list"

RAW_DIR = PROJECT_ROOT / "research/raw/iiko/inventory"
RAW_PRODUCTS_PATH = PROJECT_ROOT / "research/raw/iiko/products_list.json"
PROCESSED_DIR = PROJECT_ROOT / "research/processed/iiko/inventory"
WHITELIST_PATH = PROJECT_ROOT / "research/processed/economic_block/pnl_product_whitelist.csv"

INVENTORY_MONTHLY_CSV = PROCESSED_DIR / "inventory_monthly.csv"
PNL_LINE_MONTHLY_CSV = PROCESSED_DIR / "pnl_line_monthly.csv"
UNCLASSIFIED_CSV = PROCESSED_DIR / "unclassified_documents.csv"
REPORT_PATH = PROCESSED_DIR / "report.md"

DEFAULT_START_MONTH = "2025-11"
DEFAULT_END_MONTH = "2026-05"

REVISION_LINE = "Результаты ревизии"
PACKAGING_LINE = "Результаты инвентаризации упаковки (кроме коробок для пиццы)"
PIZZA_BOX_LINE = "Результаты инвентаризации коробки д/пиццы"
TARGET_LINES = [REVISION_LINE, PACKAGING_LINE, PIZZA_BOX_LINE]

INVENTORY_FIELDS = [
    "month",
    "pnl_line",
    "document_id",
    "document_date",
    "document_num",
    "signed_sum",
    "classification_method",
    "confidence",
]
PNL_FIELDS = ["month", "pnl_line", "total_sum", "document_count"]
UNCLASSIFIED_FIELDS = [
    "month",
    "document_id",
    "document_date",
    "document_num",
    "document_sum",
    "classification_guess",
    "confidence",
    "packaging_ratio",
    "raw_material_ratio",
    "product_line_count",
    "whitelist_match_count",
    "raw_keyword_count",
    "requires_review_reason",
    "sample_products",
]

RAW_MATERIAL_PATTERNS = [
    r"\bрис\b",
    "мук",
    "молок",
    "слив",
    "сметан",
    "творог",
    "сыр",
    "моцарел",
    "пармезан",
    "чеддер",
    "крев",
    "мяс",
    "говяд",
    "свин",
    "кур",
    "цып",
    "рыб",
    "лосос",
    "семг",
    "сёмг",
    "тунец",
    "угор",
    "икра",
    "краб",
    "нори",
    "яйц",
    "карто",
    "морков",
    r"\bлук\b",
    "огур",
    "томат",
    "помид",
    "перец",
    "капуст",
    "салат",
    "гриб",
    "шамп",
    "бекон",
    "ветчин",
    "колбас",
    "фарш",
    "филе",
    "груд",
    "масло",
    "майонез",
    "кетчуп",
    "сахар",
    "соль",
    "уксус",
    "имбир",
    "васаби",
    "кунж",
    "сухар",
    "лапша",
    "чеснок",
    "зелень",
    "укроп",
    "петруш",
    "ананас",
    "кукуруз",
    "фасол",
    "авокад",
    "дрожж",
    "тесто",
    "соев",
    "терияки",
    "кимчи",
]

PACKAGING_CONTEXT_PATTERNS = [
    "ланч",
    "бокс",
    "короб",
    "пакет",
    "крафт",
    "соусник",
    "контейнер",
    "крыш",
    "салфет",
    "зубочист",
    "палоч",
    "бутыл",
    "зип",
    "дип-пот",
    "оберт",
    "обёрт",
    "вилк",
    "ложк",
    "фольг",
    "пленк",
    "плёнк",
    "перчат",
    "лента",
    "чеков",
    "шпаж",
    "вакуум",
    "фасов",
    "мешок",
]


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
class WhitelistSets:
    packaging_include_ids: set[str]
    packaging_include_names: set[str]
    packaging_review_ids: set[str]
    packaging_review_names: set[str]
    pizza_box_ids: set[str]
    pizza_box_names: set[str]
    known_packaging_ids: set[str]
    known_packaging_names: set[str]
    entries: list[WhitelistEntry]


@dataclass(frozen=True)
class InventoryItem:
    month: str
    document_id: str
    document_date: dt.date | None
    document_num: str
    product_id: str
    product_code: str
    product_name: str
    signed_sum: Decimal
    row: dict[str, Any]


@dataclass(frozen=True)
class ClassifiedDocument:
    month: str
    document_id: str
    document_date: dt.date | None
    document_num: str
    items: list[InventoryItem]
    document_sum: Decimal
    classification: str
    confidence: float
    classification_method: str
    packaging_ratio: float
    raw_material_ratio: float
    whitelist_match_count: int
    raw_keyword_count: int
    requires_review_reason: str


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

    periods: list[tuple[str, dt.date, dt.date]] = []
    current = start
    while current <= end:
        periods.append((month_key(current), current, month_end(current)))
        current = next_month(current)
    return periods


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


def local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def parse_iiko_date(value: Any) -> dt.date | None:
    text = clean_text(value)
    if not text:
        return None
    for fmt, length in (("%d.%m.%Y", 10), ("%Y-%m-%d", 10), ("%Y-%m-%dT%H:%M:%S", 19)):
        try:
            return dt.datetime.strptime(text[:length], fmt).date()
        except ValueError:
            pass
    return None


def parse_store_operations(data: bytes) -> tuple[str, list[dict[str, Any]]]:
    stripped = data.lstrip()
    if stripped.startswith((b"{", b"[")):
        return "json", parse_json_rows(data)

    text = data.decode("utf-8", "replace")
    row_pattern = re.compile(
        r"<(?P<tag>(?:[\w.-]+:)?storeReportItemDto)\b[^>]*>(?P<body>.*?)</(?P=tag)>",
        re.IGNORECASE | re.DOTALL,
    )
    cell_pattern = re.compile(
        r"<(?P<tag>(?:[\w.-]+:)?[A-Za-z_][\w.:-]*)(?:\s[^>]*)?>(?P<value>.*?)</(?P=tag)>"
        r"|<(?P<empty>(?:[\w.-]+:)?[A-Za-z_][\w.:-]*)(?:\s[^>]*)?/>",
        re.DOTALL,
    )

    rows: list[dict[str, Any]] = []
    for match in row_pattern.finditer(text):
        body = match.group("body")
        row: dict[str, Any] = {}
        for cell in cell_pattern.finditer(body):
            raw_tag = cell.group("tag") or cell.group("empty")
            if not raw_tag:
                continue
            key = local_name(raw_tag)
            value = ""
            if cell.group("tag"):
                value = html.unescape(re.sub(r"<[^>]+>", "", cell.group("value") or ""))
            row[key] = clean_text(value)
        if not row:
            continue
        rows.append(row)
    return "xml", rows


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def sanitize_message(value: str) -> str:
    text = clean_text(value)
    text = re.sub(r"([?&](?:key|pass|password|login)=)[^&\s\"']+", r"\1<redacted>", text)
    text = re.sub(r"([?&][A-Za-z_]*token=)[^&\s\"']+", r"\1<redacted>", text, flags=re.I)
    text = re.sub(r"\b[0-9a-f]{40}\b", "<sha1-redacted>", text, flags=re.I)
    return text[:1000]


def load_whitelist(path: Path) -> WhitelistSets:
    entries: list[WhitelistEntry] = []
    packaging_include_ids: set[str] = set()
    packaging_include_names: set[str] = set()
    packaging_review_ids: set[str] = set()
    packaging_review_names: set[str] = set()
    pizza_box_ids: set[str] = set()
    pizza_box_names: set[str] = set()

    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if clean_text(row.get("source_endpoint")) != INVENTORY_ENDPOINT:
                continue
            entry = WhitelistEntry(
                pnl_line=clean_text(row.get("pnl_line")),
                component=clean_text(row.get("component")),
                excel_group=clean_text(row.get("excel_group")),
                excel_row=clean_text(row.get("excel_row")),
                include_status=clean_text(row.get("include_status")),
                product_id=clean_text(row.get("iiko_product_id")),
                product_code=clean_text(row.get("iiko_product_code")),
                product_name=clean_text(row.get("iiko_product_name")),
                march_api_sum=decimal_value(row.get("march_2026_api_sum")),
                march_excel_group_sum=nullable_decimal(row.get("march_2026_excel_group_sum")),
                control_status=clean_text(row.get("control_status")),
            )
            entries.append(entry)
            if entry.pnl_line != PACKAGING_LINE:
                continue

            normalized_name = normalize_name(entry.product_name)
            if entry.include_status == "include":
                packaging_include_ids.add(entry.product_id)
                packaging_include_names.add(normalized_name)
            elif entry.include_status == "requires_owner_review":
                packaging_review_ids.add(entry.product_id)
                packaging_review_names.add(normalized_name)
            elif entry.include_status == "exclude" and (
                entry.excel_group == "коробки для пиццы" or "короб" in normalized_name and "пицц" in normalized_name
            ):
                pizza_box_ids.add(entry.product_id)
                pizza_box_names.add(normalized_name)

    known_packaging_ids = packaging_include_ids | packaging_review_ids | pizza_box_ids
    known_packaging_names = packaging_include_names | packaging_review_names | pizza_box_names
    return WhitelistSets(
        packaging_include_ids=packaging_include_ids,
        packaging_include_names=packaging_include_names,
        packaging_review_ids=packaging_review_ids,
        packaging_review_names=packaging_review_names,
        pizza_box_ids=pizza_box_ids,
        pizza_box_names=pizza_box_names,
        known_packaging_ids=known_packaging_ids,
        known_packaging_names=known_packaging_names,
        entries=entries,
    )


def product_records_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("rows", "data", "items", "records", "result", "products"):
            if key in payload:
                return product_records_from_payload(payload[key])
    return []


def load_products(client: IikoClient | None, refresh: bool = False) -> dict[str, dict[str, str]]:
    if RAW_PRODUCTS_PATH.exists() and not refresh:
        payload = json.loads(RAW_PRODUCTS_PATH.read_text(encoding="utf-8"))
    else:
        if client is None:
            raise SystemExit(f"{rel(RAW_PRODUCTS_PATH)} is missing and no iiko client is available")
        status, data = client.request(PRODUCTS_ENDPOINT)
        payload = json.loads(data.decode("utf-8"))
        RAW_PRODUCTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        RAW_PRODUCTS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"fetched {PRODUCTS_ENDPOINT}: status={status}")
        time.sleep(0.15)

    products: dict[str, dict[str, str]] = {}
    for row in product_records_from_payload(payload):
        product_id = clean_text(value_from(row, ["id", "product", "productId"]))
        if not product_id:
            continue
        products[product_id] = {
            "id": product_id,
            "code": clean_text(value_from(row, ["code", "num"])),
            "name": clean_text(value_from(row, ["name", "productName"])),
        }
    return products


def raw_month_path(month: str) -> Path:
    return RAW_DIR / f"{month}.json"


def inventory_params(start: dt.date, end: dt.date) -> dict[str, str]:
    return {
        "dateFrom": start.strftime("%d.%m.%Y"),
        "dateTo": end.strftime("%d.%m.%Y"),
        "documentTypes": "INCOMING_INVENTORY",
        "productDetalization": "true",
        "showCostCorrections": "false",
    }


def fetch_inventory_month(
    client: IikoClient | None,
    month: str,
    start: dt.date,
    end: dt.date,
    *,
    reuse_raw: bool = False,
) -> list[dict[str, Any]]:
    path = raw_month_path(month)
    if reuse_raw and path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [row for row in payload.get("rows", []) if isinstance(row, dict)]

    if client is None:
        raise SystemExit(f"{rel(path)} is missing and no iiko client is available")

    params = inventory_params(start, end)
    try:
        status, data = client.request(INVENTORY_ENDPOINT, params=params)
        response_format, rows = parse_store_operations(data)
        payload = {
            "endpoint": INVENTORY_ENDPOINT,
            "params": params,
            "status": status,
            "response_format": response_format,
            "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
            "row_count": len(rows),
            "rows": rows,
        }
        write_json(path, payload)
        print(f"fetched {month}: {len(rows)} inventory rows")
        time.sleep(0.15)
        return rows
    except (IikoHTTPError, TimeoutError, OSError, ValueError) as raw_exc:
        status = raw_exc.status if isinstance(raw_exc, IikoHTTPError) else None
        message = raw_exc.message if isinstance(raw_exc, IikoHTTPError) else str(raw_exc)
        payload = {
            "endpoint": INVENTORY_ENDPOINT,
            "params": params,
            "status": status,
            "error": sanitize_message(message),
            "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
            "row_count": 0,
            "rows": [],
        }
        write_json(path, payload)
        print(f"{month}: failed to fetch inventory, status={status}")
        return []


def inventory_item_from_row(month: str, row: dict[str, Any], products: dict[str, dict[str, str]]) -> InventoryItem | None:
    document_type = clean_text(value_from(row, ["documentType", "document_type"]))
    if document_type and document_type != "INCOMING_INVENTORY":
        return None

    document_id = clean_text(value_from(row, ["documentId", "document_id", "document"]))
    if not document_id:
        return None

    product_id = clean_text(value_from(row, ["product", "productId", "product_id"]))
    product_ref = products.get(product_id, {})
    product_name = clean_text(
        value_from(row, ["productName", "product.name", "name"]) or product_ref.get("name", "")
    )
    product_code = clean_text(value_from(row, ["productCode", "product.code", "code"]) or product_ref.get("code", ""))
    signed_sum = decimal_value(value_from(row, ["sum", "cost", "documentSum"]))

    return InventoryItem(
        month=month,
        document_id=document_id,
        document_date=parse_iiko_date(value_from(row, ["date", "documentDate"])),
        document_num=clean_text(value_from(row, ["documentNum", "documentNumber", "number"])),
        product_id=product_id,
        product_code=product_code,
        product_name=product_name,
        signed_sum=signed_sum,
        row=row,
    )


def group_documents(
    month_rows: dict[str, list[dict[str, Any]]],
    products: dict[str, dict[str, str]],
) -> dict[str, list[InventoryItem]]:
    documents: dict[str, list[InventoryItem]] = defaultdict(list)
    for month, rows in month_rows.items():
        for row in rows:
            item = inventory_item_from_row(month, row, products)
            if item is not None:
                documents[item.document_id].append(item)
    return dict(documents)


def item_name(item: InventoryItem) -> str:
    return normalize_name(item.product_name)


def product_id_or_name_in(item: InventoryItem, ids: set[str], names: set[str]) -> bool:
    return bool((item.product_id and item.product_id in ids) or (item_name(item) in names))


def is_raw_material_name(product_name: str) -> bool:
    normalized = normalize_name(product_name)
    if not normalized:
        return False
    return any(re.search(pattern, normalized) for pattern in RAW_MATERIAL_PATTERNS)


def is_packaging_context_name(product_name: str) -> bool:
    normalized = normalize_name(product_name)
    if not normalized:
        return False
    return any(pattern in normalized for pattern in PACKAGING_CONTEXT_PATTERNS)


def document_date(items: list[InventoryItem]) -> dt.date | None:
    for item in items:
        if item.document_date is not None:
            return item.document_date
    return None


def document_num(items: list[InventoryItem]) -> str:
    for item in items:
        if item.document_num:
            return item.document_num
    return ""


def classify_document(items: list[InventoryItem], whitelist: WhitelistSets) -> ClassifiedDocument:
    total = len(items)
    month = items[0].month if items else ""
    doc_id = items[0].document_id if items else ""
    doc_date = document_date(items)
    doc_num = document_num(items)
    document_sum = sum((item.signed_sum for item in items), Decimal("0"))

    whitelist_match_count = sum(
        1
        for item in items
        if product_id_or_name_in(item, whitelist.known_packaging_ids, whitelist.known_packaging_names)
    )
    packaging_context_count = sum(
        1
        for item in items
        if product_id_or_name_in(item, whitelist.known_packaging_ids, whitelist.known_packaging_names)
        or is_packaging_context_name(item.product_name)
    )
    raw_keyword_count = sum(
        1
        for item in items
        if not product_id_or_name_in(item, whitelist.known_packaging_ids, whitelist.known_packaging_names)
        and not is_packaging_context_name(item.product_name)
        and is_raw_material_name(item.product_name)
    )

    whitelist_ratio = whitelist_match_count / total if total else 0.0
    packaging_ratio = packaging_context_count / total if total else 0.0
    raw_material_ratio = raw_keyword_count / total if total else 0.0
    weekday_bonus = bool(doc_date and doc_date.weekday() in {0, 1})

    if whitelist_ratio >= 0.80 or packaging_ratio >= 0.80:
        confidence = packaging_ratio
        return ClassifiedDocument(
            month=month,
            document_id=doc_id,
            document_date=doc_date,
            document_num=doc_num,
            items=items,
            document_sum=document_sum,
            classification="packaging",
            confidence=confidence,
            classification_method=(
                f"packaging_context_ratio={packaging_ratio:.3f}; "
                f"whitelist_packaging_ratio={whitelist_ratio:.3f}; "
                f"context_matches={packaging_context_count}/{total}; "
                f"whitelist_matches={whitelist_match_count}/{total}; threshold=0.80"
            ),
            packaging_ratio=packaging_ratio,
            raw_material_ratio=raw_material_ratio,
            whitelist_match_count=whitelist_match_count,
            raw_keyword_count=raw_keyword_count,
            requires_review_reason="",
        )

    if raw_material_ratio >= 0.50:
        confidence = max(raw_material_ratio, 0.75 if weekday_bonus else raw_material_ratio)
        return ClassifiedDocument(
            month=month,
            document_id=doc_id,
            document_date=doc_date,
            document_num=doc_num,
            items=items,
            document_sum=document_sum,
            classification="product_revision",
            confidence=confidence,
            classification_method=(
                f"raw_material_ratio={raw_material_ratio:.3f}; "
                f"raw_matches={raw_keyword_count}/{total}; "
                f"weekday={'mon/tue' if weekday_bonus else 'other'}; threshold=0.50"
            ),
            packaging_ratio=packaging_ratio,
            raw_material_ratio=raw_material_ratio,
            whitelist_match_count=whitelist_match_count,
            raw_keyword_count=raw_keyword_count,
            requires_review_reason="",
        )

    confidence = max(packaging_ratio, raw_material_ratio)
    reasons = []
    if packaging_ratio > 0:
        reasons.append(f"packaging_context_ratio={packaging_ratio:.3f}<0.80")
    if raw_material_ratio > 0:
        reasons.append(f"raw_material_ratio={raw_material_ratio:.3f}<0.50")
    if not reasons:
        reasons.append("no whitelist or raw-material context")
    return ClassifiedDocument(
        month=month,
        document_id=doc_id,
        document_date=doc_date,
        document_num=doc_num,
        items=items,
        document_sum=document_sum,
        classification="requires_review",
        confidence=confidence,
        classification_method="requires_review: " + "; ".join(reasons),
        packaging_ratio=packaging_ratio,
        raw_material_ratio=raw_material_ratio,
        whitelist_match_count=whitelist_match_count,
        raw_keyword_count=raw_keyword_count,
        requires_review_reason="; ".join(reasons),
    )


def sum_items_by_set(items: list[InventoryItem], ids: set[str], names: set[str]) -> Decimal:
    return sum(
        (item.signed_sum for item in items if product_id_or_name_in(item, ids, names)),
        Decimal("0"),
    )


def build_inventory_rows(classified_documents: list[ClassifiedDocument], whitelist: WhitelistSets) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for document in sorted(classified_documents, key=document_sort_key):
        base = {
            "month": document.month,
            "document_id": document.document_id,
            "document_date": document.document_date.isoformat() if document.document_date else "",
            "document_num": document.document_num,
            "classification_method": document.classification_method,
            "confidence": f"{document.confidence:.3f}",
        }
        if document.classification == "packaging":
            packaging_sum = sum_items_by_set(
                document.items, whitelist.packaging_include_ids, whitelist.packaging_include_names
            )
            pizza_box_sum = sum_items_by_set(document.items, whitelist.pizza_box_ids, whitelist.pizza_box_names)
            rows.append({**base, "pnl_line": PACKAGING_LINE, "signed_sum": money_text(packaging_sum)})
            rows.append({**base, "pnl_line": PIZZA_BOX_LINE, "signed_sum": money_text(pizza_box_sum)})
        elif document.classification == "product_revision":
            rows.append({**base, "pnl_line": REVISION_LINE, "signed_sum": money_text(document.document_sum)})
    return rows


def document_sort_key(document: ClassifiedDocument) -> tuple[str, str, str]:
    date_text = document.document_date.isoformat() if document.document_date else ""
    return (document.month, date_text, document.document_num)


def build_pnl_rows(inventory_rows: list[dict[str, Any]], periods: list[tuple[str, dt.date, dt.date]]) -> list[dict[str, Any]]:
    totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    documents: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in inventory_rows:
        key = (row["month"], row["pnl_line"])
        totals[key] += decimal_value(row["signed_sum"])
        if row.get("document_id"):
            documents[key].add(row["document_id"])

    rows: list[dict[str, Any]] = []
    for month, _, _ in periods:
        for pnl_line in TARGET_LINES:
            key = (month, pnl_line)
            rows.append(
                {
                    "month": month,
                    "pnl_line": pnl_line,
                    "total_sum": money_text(totals.get(key, Decimal("0"))),
                    "document_count": len(documents.get(key, set())),
                }
            )
    return rows


def sample_products(items: list[InventoryItem], limit: int = 8) -> str:
    names: list[str] = []
    seen: set[str] = set()
    for item in items:
        name = clean_text(item.product_name)
        if not name or name in seen:
            continue
        names.append(name)
        seen.add(name)
        if len(names) >= limit:
            break
    return "; ".join(names)


def build_unclassified_rows(classified_documents: list[ClassifiedDocument]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for document in sorted(classified_documents, key=document_sort_key):
        if document.confidence >= 0.70:
            continue
        rows.append(
            {
                "month": document.month,
                "document_id": document.document_id,
                "document_date": document.document_date.isoformat() if document.document_date else "",
                "document_num": document.document_num,
                "document_sum": money_text(document.document_sum),
                "classification_guess": document.classification,
                "confidence": f"{document.confidence:.3f}",
                "packaging_ratio": f"{document.packaging_ratio:.3f}",
                "raw_material_ratio": f"{document.raw_material_ratio:.3f}",
                "product_line_count": len(document.items),
                "whitelist_match_count": document.whitelist_match_count,
                "raw_keyword_count": document.raw_keyword_count,
                "requires_review_reason": document.requires_review_reason or document.classification_method,
                "sample_products": sample_products(document.items),
            }
        )
    return rows


def format_markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    result = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        result.append("| " + " | ".join(str(cell).replace("\n", " ") for cell in row) + " |")
    return result


def totals_from_pnl_rows(pnl_rows: list[dict[str, Any]]) -> dict[tuple[str, str], Decimal]:
    return {(row["month"], row["pnl_line"]): decimal_value(row["total_sum"]) for row in pnl_rows}


def march_control(
    classified_documents: list[ClassifiedDocument],
    whitelist: WhitelistSets,
) -> dict[str, Any]:
    packaging_documents = [
        document
        for document in classified_documents
        if document.month == "2026-03" and document.classification == "packaging"
    ]
    target = next(
        (
            document
            for document in packaging_documents
            if document.document_num == "0012"
            and document.document_date == dt.date(2026, 3, 2)
        ),
        packaging_documents[0] if packaging_documents else None,
    )
    if target is None:
        return {
            "found": False,
            "document": None,
            "packaging_sum": Decimal("0"),
            "pizza_box_sum": Decimal("0"),
            "excel_packaging_sum": Decimal("0"),
            "delta": Decimal("0"),
            "delta_pct": None,
            "group_rows": [],
            "review_rows": [],
        }

    packaging_sum = sum_items_by_set(target.items, whitelist.packaging_include_ids, whitelist.packaging_include_names)
    pizza_box_sum = sum_items_by_set(target.items, whitelist.pizza_box_ids, whitelist.pizza_box_names)

    excel_by_group: dict[str, Decimal] = {}
    for entry in whitelist.entries:
        if entry.pnl_line != PACKAGING_LINE or entry.include_status == "exclude":
            continue
        if entry.march_excel_group_sum is not None:
            excel_by_group[entry.excel_group] = entry.march_excel_group_sum
    excel_packaging_sum = sum(excel_by_group.values(), Decimal("0"))
    delta = packaging_sum - excel_packaging_sum
    delta_pct = abs(delta / excel_packaging_sum) if excel_packaging_sum else None

    item_sums_by_id: dict[str, Decimal] = defaultdict(Decimal)
    item_sums_by_name: dict[str, Decimal] = defaultdict(Decimal)
    for item in target.items:
        if item.product_id:
            item_sums_by_id[item.product_id] += item.signed_sum
        item_sums_by_name[item_name(item)] += item.signed_sum

    by_group_status: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in whitelist.entries:
        if entry.pnl_line != PACKAGING_LINE or entry.include_status == "exclude":
            continue
        key = (entry.excel_group, entry.include_status)
        target_row = by_group_status.setdefault(
            key,
            {
                "excel_group": entry.excel_group,
                "excel_rows": set(),
                "include_status": entry.include_status,
                "api_sum": Decimal("0"),
                "excel_sum": entry.march_excel_group_sum,
                "products": [],
            },
        )
        if entry.product_id:
            actual = item_sums_by_id.get(entry.product_id, Decimal("0"))
        else:
            actual = item_sums_by_name.get(normalize_name(entry.product_name), Decimal("0"))
        target_row["api_sum"] += actual
        target_row["excel_rows"].add(entry.excel_row)
        target_row["products"].append(entry.product_name)

    group_rows = []
    review_rows = []
    for row in by_group_status.values():
        excel_sum = row["excel_sum"]
        delta_group = row["api_sum"] - excel_sum if excel_sum is not None else Decimal("0")
        rendered = {
            "excel_group": row["excel_group"],
            "excel_rows": ",".join(sorted(row["excel_rows"])),
            "include_status": row["include_status"],
            "api_sum": row["api_sum"],
            "excel_sum": excel_sum,
            "delta": delta_group,
        }
        group_rows.append(rendered)
        if row["include_status"] == "requires_owner_review":
            review_rows.append(rendered)

    return {
        "found": True,
        "document": target,
        "packaging_sum": packaging_sum,
        "pizza_box_sum": pizza_box_sum,
        "excel_packaging_sum": excel_packaging_sum,
        "delta": delta,
        "delta_pct": delta_pct,
        "group_rows": sorted(group_rows, key=lambda row: row["excel_rows"]),
        "review_rows": sorted(review_rows, key=lambda row: row["excel_rows"]),
    }


def review_hits(classified_documents: list[ClassifiedDocument], whitelist: WhitelistSets) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for document in sorted(classified_documents, key=document_sort_key):
        if document.classification != "packaging":
            continue
        for item in document.items:
            if not product_id_or_name_in(item, whitelist.packaging_review_ids, whitelist.packaging_review_names):
                continue
            rows.append(
                {
                    "month": document.month,
                    "document_date": document.document_date.isoformat() if document.document_date else "",
                    "document_num": document.document_num,
                    "product_name": item.product_name,
                    "signed_sum": item.signed_sum,
                }
            )
    return rows


def write_report(
    periods: list[tuple[str, dt.date, dt.date]],
    classified_documents: list[ClassifiedDocument],
    inventory_rows: list[dict[str, Any]],
    pnl_rows: list[dict[str, Any]],
    unclassified_rows: list[dict[str, Any]],
    whitelist: WhitelistSets,
) -> None:
    totals = totals_from_pnl_rows(pnl_rows)
    control = march_control(classified_documents, whitelist)
    review_rows = review_hits(classified_documents, whitelist)

    lines = [
        "# iiko inventory results",
        "",
        f"Сформировано: {dt.datetime.now().isoformat(timespec='seconds')}.",
        "",
        "Источник: `GET /resto/api/reports/storeOperations` с `documentTypes=INCOMING_INVENTORY`, "
        "`productDetalization=true`, `showCostCorrections=false`.",
        "",
        "## Периоды и raw",
        "",
    ]
    period_rows = [
        [month, start.isoformat(), end.isoformat(), f"`{rel(raw_month_path(month))}`"]
        for month, start, end in periods
    ]
    lines.extend(format_markdown_table(["month", "from", "to", "raw"], period_rows))

    lines.extend(["", "## Суммы по строкам ОПиУ", ""])
    total_rows: list[list[str]] = []
    for month, _, _ in periods:
        for pnl_line in TARGET_LINES:
            total_rows.append(
                [
                    month,
                    pnl_line,
                    money_human(totals.get((month, pnl_line), Decimal("0"))),
                    str(
                        next(
                            (
                                row["document_count"]
                                for row in pnl_rows
                                if row["month"] == month and row["pnl_line"] == pnl_line
                            ),
                            0,
                        )
                    ),
                ]
            )
    lines.extend(format_markdown_table(["month", "pnl_line", "total_sum", "document_count"], total_rows))

    lines.extend(["", "## Март 2026: контроль упаковки 0012", ""])
    if control["found"]:
        document = control["document"]
        assert isinstance(document, ClassifiedDocument)
        delta_pct_text = "" if control["delta_pct"] is None else f"{control['delta_pct'] * Decimal('100'):.2f}%"
        status = "ok" if control["delta_pct"] is not None and control["delta_pct"] <= Decimal("0.02") else "requires_review"
        lines.extend(
            [
                f"Документ `{document.document_num}` от `{document.document_date.isoformat() if document.document_date else ''}` найден; "
                f"товарных строк: `{len(document.items)}`.",
                "",
            ]
        )
        lines.extend(
            format_markdown_table(
                ["check", "value"],
                [
                    ["Упаковка include без коробок", money_human(control["packaging_sum"])],
                    ["Коробки д/пиццы, 4 SKU", money_human(control["pizza_box_sum"])],
                    ["Excel упаковка A25:B47", money_human(control["excel_packaging_sum"])],
                    ["delta", money_human(control["delta"])],
                    ["delta_pct", delta_pct_text],
                    ["status", status],
                ],
            )
        )
    else:
        lines.append("Документ упаковки `0012` от `02.03.2026` не найден.")

    lines.extend(["", "### Контрольные группы марта", ""])
    group_rows = [
        [
            row["excel_rows"],
            row["excel_group"],
            row["include_status"],
            money_human(row["api_sum"]),
            money_human(row["excel_sum"] or Decimal("0")),
            money_human(row["delta"]),
        ]
        for row in control["group_rows"]
    ]
    lines.extend(
        format_markdown_table(
            ["rows", "excel_group", "status", "api_sum", "excel_sum", "delta"],
            group_rows,
        )
        if group_rows
        else ["Контрольные группы не собраны."]
    )

    lines.extend(["", "## requires_owner_review не включено в суммы", ""])
    if review_rows:
        rendered_review_rows = [
            [
                row["month"],
                row["document_date"],
                row["document_num"],
                row["product_name"],
                money_human(row["signed_sum"]),
            ]
            for row in review_rows
        ]
        lines.extend(format_markdown_table(["month", "date", "doc", "product", "signed_sum"], rendered_review_rows))
    else:
        lines.append("Попаданий `requires_owner_review` в найденных документах упаковки нет.")

    lines.extend(["", "## Спорные документы", ""])
    if unclassified_rows:
        rendered_unclassified = [
            [
                row["month"],
                row["document_date"],
                row["document_num"],
                row["classification_guess"],
                row["confidence"],
                row["packaging_ratio"],
                row["raw_material_ratio"],
                row["sample_products"],
            ]
            for row in unclassified_rows[:100]
        ]
        lines.extend(
            format_markdown_table(
                ["month", "date", "doc", "guess", "confidence", "packaging", "raw", "sample_products"],
                rendered_unclassified,
            )
        )
    else:
        lines.append("Документов с confidence < 0.70 нет.")

    lines.extend(["", "## Классификация документов", ""])
    class_counts: dict[tuple[str, str], int] = defaultdict(int)
    for document in classified_documents:
        class_counts[(document.month, document.classification)] += 1
    class_rows = [
        [month, classification, str(count)]
        for (month, classification), count in sorted(class_counts.items())
    ]
    lines.extend(format_markdown_table(["month", "classification", "document_count"], class_rows))

    lines.extend(
        [
            "",
            "## Выходные файлы",
            "",
            f"- `{rel(INVENTORY_MONTHLY_CSV)}`",
            f"- `{rel(PNL_LINE_MONTHLY_CSV)}`",
            f"- `{rel(UNCLASSIFIED_CSV)}`",
            f"- `{rel(REPORT_PATH)}`",
            "",
            "## Примечания",
            "",
            "- `requires_owner_review` используется для распознавания документа упаковки, но не включается в P&L суммы.",
            "- Коробки для пиццы берутся только по четырем SKU из whitelist и выводятся отдельной строкой.",
        ]
    )
    today = dt.date.today()
    if periods[-1][2] > today:
        lines.append(
            f"- Последний период запрошен до `{periods[-1][2].isoformat()}`; "
            f"на дату запуска `{today.isoformat()}` месяц предварительный."
        )

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-month", default=DEFAULT_START_MONTH, help="YYYY-MM, default: 2025-11")
    parser.add_argument("--end-month", default=DEFAULT_END_MONTH, help="YYYY-MM, default: 2026-05")
    parser.add_argument("--refresh-products", action="store_true", help="Refresh products_list.json from iiko")
    parser.add_argument("--reuse-raw", action="store_true", help="Use existing research/raw/iiko/inventory/<month>.json files")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    periods = month_periods(args.start_month, args.end_month)
    load_local_env()
    whitelist = load_whitelist(WHITELIST_PATH)
    client: IikoClient | None = None
    if args.refresh_products or not RAW_PRODUCTS_PATH.exists() or not args.reuse_raw:
        client = IikoClient()
    products = load_products(client, refresh=args.refresh_products)
    if client is None and not args.reuse_raw:
        client = IikoClient()

    month_rows: dict[str, list[dict[str, Any]]] = {}
    for month, start, end in periods:
        month_rows[month] = fetch_inventory_month(client, month, start, end, reuse_raw=args.reuse_raw)

    documents = group_documents(month_rows, products)
    classified_documents = [classify_document(items, whitelist) for items in documents.values() if items]
    inventory_rows = build_inventory_rows(classified_documents, whitelist)
    pnl_rows = build_pnl_rows(inventory_rows, periods)
    unclassified_rows = build_unclassified_rows(classified_documents)

    write_csv(INVENTORY_MONTHLY_CSV, inventory_rows, INVENTORY_FIELDS)
    write_csv(PNL_LINE_MONTHLY_CSV, pnl_rows, PNL_FIELDS)
    write_csv(UNCLASSIFIED_CSV, unclassified_rows, UNCLASSIFIED_FIELDS)
    write_report(periods, classified_documents, inventory_rows, pnl_rows, unclassified_rows, whitelist)

    print(f"wrote {rel(INVENTORY_MONTHLY_CSV)}: {len(inventory_rows)} rows")
    print(f"wrote {rel(PNL_LINE_MONTHLY_CSV)}: {len(pnl_rows)} rows")
    print(f"wrote {rel(UNCLASSIFIED_CSV)}: {len(unclassified_rows)} rows")
    print(f"wrote {rel(REPORT_PATH)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

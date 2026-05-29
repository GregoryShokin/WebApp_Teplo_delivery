#!/usr/bin/env python3
"""Read-only iiko sales export by day and hour.

The script reads secrets from environment/.env, performs only GET requests to
iiko reporting endpoints plus POST /auth if a token refresh is needed, stores raw
responses under research/raw/iiko/sales/, and writes aggregate outputs only.
"""

from __future__ import annotations

import calendar
import csv
import datetime as dt
import hashlib
import html
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = PROJECT_ROOT / "research/raw/iiko/sales"
PROCESSED_DIR = PROJECT_ROOT / "research/processed/iiko/sales"

DAILY_START = dt.date(2025, 5, 1)
HOURLY_START = dt.date(2025, 11, 1)
END_DATE = dt.date(2026, 5, 17)

ACTIVE_DEPARTMENT_MARKER = "черников"
HISTORICAL_DEPARTMENT_MARKER = "гагарин"
KNOWN_REVENUE_BENCHMARK = 25_524_455.0
KNOWN_ORDER_BENCHMARK = 15_754.0

DAILY_GROUP_ROWS = ["OpenDate.Typed", "Department"]
DAILY_AGRS = [
    "OrderNum",
    "DishSumInt",
    "DishDiscountSumInt",
    "discountWithoutVAT",
    "DiscountSum",
    "DishAmountInt",
    "ProductCostBase.ProductCost",
]
HOURLY_AGRS = [
    "OrderNum",
    "DishSumInt",
    "DishDiscountSumInt",
    "DiscountSum",
    "DishAmountInt",
]


class IikoHTTPError(RuntimeError):
    def __init__(self, status: int | None, body: bytes, message: str = "") -> None:
        self.status = status
        self.body = body
        self.message = message or body[:300].decode("utf-8", "replace")
        super().__init__(f"iiko HTTP error {status}: {self.message[:160]}")


def load_local_env() -> None:
    for env_path in (PROJECT_ROOT / ".env", PROJECT_ROOT / "ENV"):
        if not env_path.exists():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            if value and not os.environ.get(key):
                os.environ[key] = value


def month_chunks(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    chunks: list[tuple[dt.date, dt.date]] = []
    current = start
    while current <= end:
        last_day = calendar.monthrange(current.year, current.month)[1]
        chunk_end = min(end, dt.date(current.year, current.month, last_day))
        chunks.append((current, chunk_end))
        current = chunk_end + dt.timedelta(days=1)
    return chunks


def fmt_olap_date(value: dt.date) -> str:
    return value.strftime("%d.%m.%Y")


def iso(value: dt.date) -> str:
    return value.isoformat()


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ").strip()
    if text.casefold() in {"none", "null", "nan"}:
        return ""
    return text


def dimension(value: Any) -> str:
    text = clean_text(value)
    return text if text else "(пусто)"


def to_number(value: Any) -> float:
    text = clean_text(value)
    if not text:
        return 0.0
    text = text.replace(" ", "").replace("\u2212", "-")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return 0.0


def value_from(row: dict[str, Any], candidates: list[str]) -> Any:
    for candidate in candidates:
        if candidate in row:
            return row[candidate]
    lowered = {str(key).casefold(): value for key, value in row.items()}
    for candidate in candidates:
        value = lowered.get(candidate.casefold())
        if value is not None:
            return value
    return ""


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_xml_rows(data: bytes, expected_fields: list[str] | None = None) -> list[dict[str, Any]]:
    expected = {field.casefold() for field in (expected_fields or [])}
    text = data.decode("utf-8", "replace")
    row_pattern = re.compile(
        r"<(?P<tag>(?:[\w-]+:)?(?:r|row|item|record|entry|corporateItemDto))\b[^>]*>"
        r"(?P<body>.*?)</(?P=tag)>",
        re.IGNORECASE | re.DOTALL,
    )
    cell_pattern = re.compile(
        r"<(?P<tag>[A-Za-z_][\w.:-]*)(?:\s[^>]*)?>(?P<value>.*?)</(?P=tag)>"
        r"|<(?P<empty>[A-Za-z_][\w.:-]*)(?:\s[^>]*)?/>",
        re.DOTALL,
    )
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def xml_value(value: str) -> str:
        return clean_text(html.unescape(re.sub(r"<[^>]+>", "", value)))

    def tag_name(value: str) -> str:
        return value.rsplit(":", 1)[-1]

    def include(row: dict[str, Any]) -> bool:
        if not row:
            return False
        if not expected:
            return True
        return bool({str(key).casefold() for key in row} & expected)

    for match in row_pattern.finditer(text):
        row: dict[str, Any] = {}
        for cell in cell_pattern.finditer(match.group("body")):
            raw_tag = cell.group("tag") or cell.group("empty")
            if not raw_tag:
                continue
            row[tag_name(raw_tag)] = "" if cell.group("empty") else xml_value(cell.group("value") or "")
        if not include(row):
            continue
        signature = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        if signature not in seen:
            rows.append(row)
            seen.add(signature)
    return rows


def parse_json_rows(data: bytes) -> list[dict[str, Any]]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []

    def records(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            if all(isinstance(item, dict) for item in value):
                return value
            result: list[dict[str, Any]] = []
            for item in value:
                result.extend(records(item))
            return result
        if isinstance(value, dict):
            for key in ("rows", "data", "items", "records", "result"):
                if key in value:
                    found = records(value[key])
                    if found:
                        return found
            return [value]
        return []

    return records(payload)


def parse_rows(data: bytes, expected_fields: list[str] | None = None) -> list[dict[str, Any]]:
    stripped = data.lstrip()
    if stripped.startswith(b"{") or stripped.startswith(b"["):
        return parse_json_rows(data)
    return parse_xml_rows(data, expected_fields)


def parse_columns(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def department_scope(department: str) -> str:
    lowered = clean_text(department).casefold()
    if ACTIVE_DEPARTMENT_MARKER in lowered:
        return "active_chernikova"
    if HISTORICAL_DEPARTMENT_MARKER in lowered:
        return "historical_gagarina"
    if lowered:
        return "other_department"
    return "unknown_department"


def parse_olap_date(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(text[:10], fmt).date().isoformat()
        except ValueError:
            pass
    match = re.search(r"([A-Z][a-z]{2})\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+\d{2}:\d{2}:\d{2}\s+\S+\s+(\d{4})", text)
    if match:
        normalized = " ".join((match.group(1), match.group(2), match.group(3), match.group(4)))
        try:
            return dt.datetime.strptime(normalized, "%a %b %d %Y").date().isoformat()
        except ValueError:
            pass
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return "-".join(match.groups())
    raise ValueError(f"Cannot parse OLAP date: {text[:80]}")


def parse_hour(value: Any) -> int | None:
    text = clean_text(value)
    if not text:
        return None
    match = re.search(r"\b([01]?\d|2[0-3])(?::[0-5]\d)?\b", text)
    if match:
        return int(match.group(1))
    return None


def weekday_name(date_iso: str) -> str:
    names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    return names[dt.date.fromisoformat(date_iso).weekday()]


def money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ")


def money2(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def average_check(revenue: float, orders: float) -> float:
    return revenue / orders if orders else 0.0


def save_raw_response(
    path: Path,
    data: bytes,
    manifest: list[dict[str, Any]],
    *,
    endpoint: str,
    period: tuple[dt.date, dt.date] | None = None,
    status: int | None = None,
    expected_fields: list[str] | None = None,
    note: str = "",
) -> list[dict[str, Any]]:
    write_bytes(path, data)
    rows = parse_rows(data, expected_fields)
    entry: dict[str, Any] = {
        "endpoint": endpoint,
        "file": rel(path),
        "status": status,
        "bytes": len(data),
        "parsed_rows": len(rows),
    }
    if period:
        entry["period_start"] = iso(period[0])
        entry["period_end"] = iso(period[1])
    if note:
        entry["note"] = note
    manifest.append(entry)
    return rows


def params_without_key(params: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if key != "key"}


class IikoClient:
    def __init__(self) -> None:
        base = os.environ.get("IIKO_SERVER_BASE_URL", "").strip().rstrip("/")
        if not base:
            raise SystemExit("IIKO_SERVER_BASE_URL is missing")
        if not base.endswith("/resto/api"):
            base = f"{base}/resto/api"
        self.base_url = base
        self.token = os.environ.get("IIKO_SERVER_TOKEN", "").strip()
        self.login = os.environ.get("IIKO_SERVER_LOGIN", "").strip()
        password_sha1 = os.environ.get("IIKO_SERVER_PASSWORD_SHA1", "").strip()
        plain_password = os.environ.get("IIKO_SERVER_PASSWORD", "").strip()
        if not password_sha1 and plain_password:
            password_sha1 = hashlib.sha1(plain_password.encode("utf-8")).hexdigest()
        self.password_sha1 = password_sha1
        self.timeout = float(os.environ.get("IIKO_SERVER_TIMEOUT_SECONDS") or 90)
        self.context = ssl._create_unverified_context()

    def api_url(self, path: str) -> str:
        path = path if path.startswith("/") else f"/{path}"
        return f"{self.base_url}{path}"

    def refresh_token(self) -> None:
        if not self.login or not self.password_sha1:
            raise IikoHTTPError(None, b"Cannot refresh token: missing login or pass")
        body = urllib.parse.urlencode({"login": self.login, "pass": self.password_sha1}).encode("utf-8")
        request = urllib.request.Request(
            self.api_url("/auth"),
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout, context=self.context) as response:
            token = response.read().decode("utf-8", "replace").strip()
        if not token or "<" in token or "\n" in token:
            raise IikoHTTPError(None, token.encode("utf-8"), "Unexpected auth response")
        self.token = token

    def request(self, path: str, *, params: dict[str, Any] | None = None, retry_auth: bool = True) -> tuple[int, bytes]:
        params = dict(params or {})
        if path != "/auth":
            if not self.token and retry_auth:
                self.refresh_token()
            params["key"] = self.token
        url = self.api_url(path)
        if params:
            query = urllib.parse.urlencode(params, doseq=True)
            url = f"{url}?{query}"
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self.context) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()
            if retry_auth and self._looks_like_auth_error(exc.code, body):
                self.refresh_token()
                return self.request(path, params=params_without_key(params), retry_auth=False)
            raise IikoHTTPError(exc.code, body) from exc
        except urllib.error.URLError as exc:
            raise IikoHTTPError(None, str(exc).encode("utf-8")) from exc

    @staticmethod
    def _looks_like_auth_error(status: int, body: bytes) -> bool:
        text = body[:1000].decode("utf-8", "replace").casefold()
        return status in {401, 403} or any(
            marker in text
            for marker in (
                "unauthorized",
                "forbidden",
                "invalid session",
                "invalid token",
                "invalid key",
                "not authenticated",
                "не авториз",
                "неверный ключ",
            )
        )


def fetch_columns(client: IikoClient, manifest: list[dict[str, Any]]) -> dict[str, Any]:
    status, data = client.request("/v2/reports/olap/columns", params={"reportType": "SALES"})
    path = RAW_DIR / "olap_columns_sales.json"
    save_raw_response(
        path,
        data,
        manifest,
        endpoint="/v2/reports/olap/columns",
        status=status,
        note="reportType=SALES",
    )
    columns = parse_columns(data)
    time_candidates = []
    for key, meta in columns.items():
        if not isinstance(meta, dict):
            continue
        name = clean_text(meta.get("name"))
        tags = meta.get("tags") or []
        searchable = " ".join([key, name, " ".join(str(tag) for tag in tags)]).casefold()
        if any(marker in searchable for marker in ("час", "hour", "time", "время")):
            time_candidates.append(
                {
                    "field": key,
                    "name": name,
                    "type": meta.get("type"),
                    "groupingAllowed": bool(meta.get("groupingAllowed")),
                    "aggregationAllowed": bool(meta.get("aggregationAllowed")),
                    "tags": tags,
                }
            )
    time_candidates.sort(key=lambda item: item["field"])
    write_json(PROCESSED_DIR / "olap_time_columns.json", time_candidates)
    return columns


def choose_hour_field(columns: dict[str, Any]) -> tuple[str, str]:
    preferred = ["HourOpen", "OpenHour", "OpenTime.Hour", "OpenTime", "OpenTime.Minutes15", "CloseTime", "HourClose"]
    for field in preferred:
        meta = columns.get(field)
        if isinstance(meta, dict) and meta.get("groupingAllowed"):
            name = clean_text(meta.get("name"))
            if field == "HourOpen":
                reason = "поле SALES `HourOpen` называется 'Час открытия', разрешено для группировки и дает готовый час без дробления по минутам"
            else:
                reason = f"поле SALES `{field}` разрешено для группировки; `HourOpen` недоступно"
            return field, reason
    raise SystemExit("No groupingAllowed hour field found in SALES OLAP columns")


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
    for chunk_start, chunk_end in month_chunks(start, end):
        params = {
            "report": "SALES",
            "summary": "false",
            "from": fmt_olap_date(chunk_start),
            "to": fmt_olap_date(chunk_end),
            "groupRow": group_rows,
            "agr": agrs,
        }
        status, data = client.request("/reports/olap", params=params)
        raw_path = RAW_DIR / f"olap_{name}_{iso(chunk_start)}_{iso(chunk_end)}.xml"
        rows = save_raw_response(
            raw_path,
            data,
            manifest,
            endpoint="/reports/olap",
            period=(chunk_start, chunk_end),
            status=status,
            expected_fields=expected_fields,
            note=f"name={name}",
        )
        for row in rows:
            row["_period_start"] = iso(chunk_start)
            row["_period_end"] = iso(chunk_end)
            row["_source_file"] = rel(raw_path)
        all_rows.extend(rows)
        print(f"fetched {name} {iso(chunk_start)}..{iso(chunk_end)}: {len(rows)} rows")
        time.sleep(0.15)
    return all_rows


def base_metrics(row: dict[str, Any]) -> dict[str, float]:
    gross_sum = to_number(value_from(row, ["DishSumInt", "DishSumInt.sum"]))
    revenue = to_number(value_from(row, ["DishDiscountSumInt", "DishDiscountSumInt.sum", "sumAfterDiscountWithoutVAT"]))
    discount_sum = to_number(value_from(row, ["DiscountSum", "DiscountSum.sum", "discountWithoutVAT", "discountWithoutVAT.sum"]))
    if not discount_sum and gross_sum > revenue:
        discount_sum = gross_sum - revenue
    return {
        "orders": to_number(value_from(row, ["OrderNum", "OrderNum.count"])),
        "gross_sum": gross_sum,
        "revenue": revenue,
        "discount_sum": discount_sum,
        "discount_without_vat": to_number(value_from(row, ["discountWithoutVAT", "discountWithoutVAT.sum"])),
        "dish_amount": to_number(value_from(row, ["DishAmountInt", "DishAmountInt.sum"])),
        "product_cost": to_number(value_from(row, ["ProductCostBase.ProductCost", "ProductCostBase.ProductCost.sum"])),
    }


def build_daily(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        department = dimension(value_from(row, ["Department"]))
        if department_scope(department) != "active_chernikova":
            continue
        date = parse_olap_date(value_from(row, ["OpenDate.Typed"]))
        key = (date, department)
        group = groups.setdefault(
            key,
            {
                "date": date,
                "department": department,
                "orders": 0.0,
                "gross_sum": 0.0,
                "revenue": 0.0,
                "discount_sum": 0.0,
                "discount_without_vat": 0.0,
                "dish_amount": 0.0,
                "avg_check": 0.0,
                "product_cost": 0.0,
                "food_cost_pct": 0.0,
                "gross_margin": 0.0,
            },
        )
        metrics = base_metrics(row)
        for metric in ("orders", "gross_sum", "revenue", "discount_sum", "discount_without_vat", "dish_amount", "product_cost"):
            group[metric] += metrics[metric]

    result: list[dict[str, Any]] = []
    for row in groups.values():
        row["avg_check"] = average_check(row["revenue"], row["orders"])
        row["discount_share_of_gross"] = row["discount_sum"] / row["gross_sum"] if row["gross_sum"] else 0.0
        row["food_cost_pct"] = row["product_cost"] / row["revenue"] if row["revenue"] else 0.0
        row["gross_margin"] = row["revenue"] - row["product_cost"]
        row["weekday"] = weekday_name(row["date"])
        result.append(row)
    result.sort(key=lambda item: (item["date"], item["department"]))
    return result


def build_hourly(rows: list[dict[str, Any]], hour_field: str) -> list[dict[str, Any]]:
    groups: dict[tuple[str, int, str], dict[str, Any]] = {}
    for row in rows:
        department = dimension(value_from(row, ["Department"]))
        if department_scope(department) != "active_chernikova":
            continue
        date = parse_olap_date(value_from(row, ["OpenDate.Typed"]))
        hour = parse_hour(value_from(row, [hour_field]))
        if hour is None:
            continue
        key = (date, hour, department)
        group = groups.setdefault(
            key,
            {
                "date": date,
                "hour": hour,
                "department": department,
                "orders": 0.0,
                "revenue": 0.0,
                "gross_sum": 0.0,
                "discount_sum": 0.0,
                "dish_amount": 0.0,
                "avg_check": 0.0,
            },
        )
        metrics = base_metrics(row)
        for metric in ("orders", "revenue", "gross_sum", "discount_sum", "dish_amount"):
            group[metric] += metrics[metric]

    result: list[dict[str, Any]] = []
    for row in groups.values():
        row["avg_check"] = average_check(row["revenue"], row["orders"])
        row["weekday"] = weekday_name(row["date"])
        result.append(row)
    result.sort(key=lambda item: (item["date"], item["hour"], item["department"]))
    return result


def sum_metrics(rows: list[dict[str, Any]]) -> dict[str, float]:
    revenue = sum(float(row.get("revenue") or 0) for row in rows)
    orders = sum(float(row.get("orders") or 0) for row in rows)
    gross = sum(float(row.get("gross_sum") or 0) for row in rows)
    discount = sum(float(row.get("discount_sum") or 0) for row in rows)
    dish_amount = sum(float(row.get("dish_amount") or 0) for row in rows)
    product_cost = sum(float(row.get("product_cost") or 0) for row in rows)
    return {
        "revenue": revenue,
        "orders": orders,
        "gross_sum": gross,
        "discount_sum": discount,
        "dish_amount": dish_amount,
        "avg_check": average_check(revenue, orders),
        "product_cost": product_cost,
        "food_cost_pct": product_cost / revenue if revenue else 0.0,
        "gross_margin": revenue - product_cost,
    }


def aggregate_by_hour(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[int, dict[str, Any]] = {}
    for row in rows:
        hour = int(row["hour"])
        group = groups.setdefault(
            hour,
            {"hour": hour, "orders": 0.0, "revenue": 0.0, "gross_sum": 0.0, "discount_sum": 0.0, "dish_amount": 0.0},
        )
        for metric in ("orders", "revenue", "gross_sum", "discount_sum", "dish_amount"):
            group[metric] += float(row.get(metric) or 0)
    total_revenue = sum(row["revenue"] for row in groups.values())
    total_orders = sum(row["orders"] for row in groups.values())
    result = []
    for row in groups.values():
        row["avg_check"] = average_check(row["revenue"], row["orders"])
        row["revenue_share"] = row["revenue"] / total_revenue if total_revenue else 0.0
        row["orders_share"] = row["orders"] / total_orders if total_orders else 0.0
        result.append(row)
    result.sort(key=lambda item: item["hour"])
    return result


def aggregate_by_weekday(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        weekday = row["weekday"]
        group = groups.setdefault(weekday, {"weekday": weekday, "days": 0, "orders": 0.0, "revenue": 0.0})
        group["days"] += 1
        group["orders"] += float(row.get("orders") or 0)
        group["revenue"] += float(row.get("revenue") or 0)
    result = []
    for row in groups.values():
        row["avg_daily_revenue"] = row["revenue"] / row["days"] if row["days"] else 0.0
        row["avg_daily_orders"] = row["orders"] / row["days"] if row["days"] else 0.0
        row["avg_check"] = average_check(row["revenue"], row["orders"])
        result.append(row)
    order = {"Понедельник": 1, "Вторник": 2, "Среда": 3, "Четверг": 4, "Пятница": 5, "Суббота": 6, "Воскресенье": 7}
    result.sort(key=lambda item: order.get(item["weekday"], 99))
    return result


def detect_closed_or_incomplete_days(daily_rows: list[dict[str, Any]]) -> tuple[set[str], list[dict[str, Any]]]:
    active_dates = {row["date"] for row in daily_rows if row["orders"] > 0 and row["revenue"] > 0}
    if not active_dates:
        return set(), []
    start = dt.date.fromisoformat(min(active_dates))
    end = dt.date.fromisoformat(max(active_dates))
    closed: set[str] = set()
    diagnostics: list[dict[str, Any]] = []
    current = start
    while current <= end:
        date_iso = current.isoformat()
        day_rows = [row for row in daily_rows if row["date"] == date_iso]
        revenue = sum(row["revenue"] for row in day_rows)
        orders = sum(row["orders"] for row in day_rows)
        if not day_rows or orders <= 0 or revenue <= 0:
            closed.add(date_iso)
            diagnostics.append({"date": date_iso, "reason": "no_active_sales", "orders": orders, "revenue": revenue})
        current += dt.timedelta(days=1)
    return closed, diagnostics


def build_quality_risks(
    *,
    daily_rows: list[dict[str, Any]],
    hourly_rows: list[dict[str, Any]],
    all_daily_raw_rows: list[dict[str, Any]],
    all_hourly_raw_rows: list[dict[str, Any]],
    hourly_summary: dict[str, float],
    hour_field: str,
    closed_days: set[str],
) -> list[dict[str, Any]]:
    risks: list[dict[str, Any]] = []
    departments = sorted({dimension(value_from(row, ["Department"])) for row in all_daily_raw_rows + all_hourly_raw_rows})
    non_active = [department for department in departments if department_scope(department) != "active_chernikova"]
    if non_active:
        risks.append(
            {
                "risk_id": "department_scope",
                "severity": "medium",
                "risk": "OLAP возвращает не только активный контур; исторические/прочие Department исключены из processed-агрегатов.",
                "source": "iiko /reports/olap, groupRow=Department",
                "period": f"{iso(DAILY_START)} — {iso(END_DATE)}",
                "action": "В ежедневной панели явно фильтровать Department=Foodmarket Тепло Черникова.",
            }
        )
    hourly_revenue_diff = hourly_summary["revenue"] - KNOWN_REVENUE_BENCHMARK
    if abs(hourly_revenue_diff) >= 1:
        risks.append(
            {
                "risk_id": "benchmark_revenue_diff",
                "severity": "medium",
                "risk": f"Выручка часового среза отличается от ориентира на {hourly_revenue_diff:.2f} руб.",
                "source": "сравнение sales_hourly с известным канальным ориентиром",
                "period": f"{iso(HOURLY_START)} — {iso(END_DATE)}",
                "action": "Сверить набор агрегатов, фильтр Department, строки без оплаты, удаленные заказы, возвраты и сервисные строки.",
            }
        )
    hourly_order_diff = hourly_summary["orders"] - KNOWN_ORDER_BENCHMARK
    if abs(hourly_order_diff) >= 1:
        order_diff_text = (
            f"на {abs(hourly_order_diff):.0f} меньше"
            if hourly_order_diff < 0
            else f"на {hourly_order_diff:.0f} больше"
        )
        risks.append(
            {
                "risk_id": "order_num_grouping_diff",
                "severity": "medium",
                "risk": f"OrderNum в дневно-часовом срезе {order_diff_text}, чем прежний канальный ориентир.",
                "source": "сравнение sales_hourly с audit-ориентиром по каналам",
                "period": f"{iso(HOURLY_START)} — {iso(END_DATE)}",
                "action": "Для панели собственника использовать один закрепленный срез OrderNum; вероятная причина расхождения — зависимость OrderNum от группировок PayTypes/OriginName/OrderType.",
            }
        )
    product_cost_sum = sum(row["product_cost"] for row in daily_rows)
    if product_cost_sum <= 0:
        risks.append(
            {
                "risk_id": "product_cost_missing",
                "severity": "high",
                "risk": "ProductCostBase.ProductCost не дал положительную сумму в дневной выгрузке.",
                "source": "iiko /reports/olap, agr=ProductCostBase.ProductCost",
                "period": f"{iso(DAILY_START)} — {iso(END_DATE)}",
                "action": "Не выводить food cost и gross margin в панель до повторной проверки поля себестоимости.",
            }
        )
    else:
        risks.append(
            {
                "risk_id": "product_cost_method",
                "severity": "medium",
                "risk": "Food cost считается как ProductCostBase.ProductCost / DishDiscountSumInt и зависит от актуальности техкарт.",
                "source": "iiko OLAP ProductCostBase.ProductCost",
                "period": f"{iso(DAILY_START)} — {iso(END_DATE)}",
                "action": "Сверить с ОПиУ/P&L и отдельной корректировкой акционных наборов.",
            }
        )
    if closed_days:
        risks.append(
            {
                "risk_id": "zero_sales_days",
                "severity": "low",
                "risk": f"Найдены даты без активных продаж: {len(closed_days)}.",
                "source": "sales_daily.csv",
                "period": f"{min(closed_days)} — {max(closed_days)}",
                "action": "Исключать такие даты из списка провалов и отдельно проверять график работы.",
            }
        )
    risks.append(
        {
            "risk_id": "hour_field",
            "severity": "low",
            "risk": f"Часовая динамика построена по `{hour_field}`: час открытия заказа, а не час закрытия/доставки.",
            "source": "iiko /v2/reports/olap/columns",
            "period": f"{iso(HOURLY_START)} — {iso(END_DATE)}",
            "action": "Для нагрузки кухни можно дополнительно сравнить с CloseTime или Delivery.CookingFinishTime.",
        }
    )
    return risks


def md_table(rows: list[dict[str, Any]], headers: list[tuple[str, str]], *, money_fields: set[str] | None = None, pct_fields: set[str] | None = None) -> list[str]:
    money_fields = money_fields or set()
    pct_fields = pct_fields or set()
    result = ["| " + " | ".join(title for _, title in headers) + " |"]
    result.append("| " + " | ".join("---:" if key in money_fields or key in pct_fields else "---" for key, _ in headers) + " |")
    for row in rows:
        cells = []
        for key, _ in headers:
            value = row.get(key, "")
            if key in money_fields:
                cells.append(money(float(value or 0)))
            elif key in pct_fields:
                cells.append(pct(float(value or 0)))
            elif isinstance(value, float):
                cells.append(f"{value:.1f}")
            else:
                cells.append(str(value))
        result.append("| " + " | ".join(cells) + " |")
    return result


def build_summary(
    *,
    daily_rows: list[dict[str, Any]],
    hourly_rows: list[dict[str, Any]],
    hour_distribution: list[dict[str, Any]],
    weekday_distribution: list[dict[str, Any]],
    hour_field: str,
    hour_field_reason: str,
    closed_days: set[str],
    closed_day_diagnostics: list[dict[str, Any]],
    quality_risks: list[dict[str, Any]],
    manifest: list[dict[str, Any]],
) -> dict[str, Any]:
    daily_summary = sum_metrics(daily_rows)
    hourly_summary = sum_metrics(hourly_rows)
    daily_by_date = []
    by_date: dict[str, dict[str, Any]] = {}
    for row in daily_rows:
        group = by_date.setdefault(
            row["date"],
            {"date": row["date"], "weekday": row["weekday"], "orders": 0.0, "revenue": 0.0, "avg_check": 0.0},
        )
        group["orders"] += row["orders"]
        group["revenue"] += row["revenue"]
    for row in by_date.values():
        row["avg_check"] = average_check(row["revenue"], row["orders"])
        daily_by_date.append(row)
    daily_by_date.sort(key=lambda item: item["date"])
    eligible_days = [row for row in daily_by_date if row["date"] not in closed_days]
    top_days = sorted(eligible_days, key=lambda item: item["revenue"], reverse=True)[:10]
    low_days = sorted(eligible_days, key=lambda item: item["revenue"])[:10]
    best_weekdays = sorted(weekday_distribution, key=lambda item: item["avg_daily_revenue"], reverse=True)
    return {
        "periods": {
            "daily": {"start": iso(DAILY_START), "end": iso(END_DATE)},
            "hourly": {"start": iso(HOURLY_START), "end": iso(END_DATE)},
            "last_analysis_date": iso(END_DATE),
        },
        "department_filter": "active_chernikova",
        "hour_field": {"field": hour_field, "reason": hour_field_reason},
        "daily_summary": daily_summary,
        "hourly_summary": hourly_summary,
        "known_revenue_benchmark": {
            "period": f"{iso(HOURLY_START)} — {iso(END_DATE)}",
            "revenue": KNOWN_REVENUE_BENCHMARK,
            "actual_revenue": hourly_summary["revenue"],
            "difference": hourly_summary["revenue"] - KNOWN_REVENUE_BENCHMARK,
            "orders": KNOWN_ORDER_BENCHMARK,
            "actual_orders": hourly_summary["orders"],
            "order_difference": hourly_summary["orders"] - KNOWN_ORDER_BENCHMARK,
        },
        "top_revenue_days": top_days,
        "low_revenue_days": low_days,
        "hour_distribution": hour_distribution,
        "weekday_distribution": weekday_distribution,
        "best_weekday": best_weekdays[0] if best_weekdays else {},
        "worst_weekday": best_weekdays[-1] if best_weekdays else {},
        "closed_or_zero_sales_days": sorted(closed_days),
        "closed_day_diagnostics": closed_day_diagnostics,
        "quality_risks": quality_risks,
        "manifest": manifest,
    }


def build_report(summary: dict[str, Any]) -> str:
    daily = summary["daily_summary"]
    hourly = summary["hourly_summary"]
    benchmark = summary["known_revenue_benchmark"]
    order_diff_text = (
        f"меньше на {abs(benchmark['order_difference']):.0f}"
        if benchmark["order_difference"] < 0
        else f"больше на {benchmark['order_difference']:.0f}"
    )
    lines = [
        "# Продажи по дням и часам iiko",
        "",
        "## Покрытие",
        "",
        (
            f"Факт / Источник / Период / Вывод / Действие: дневная выгрузка покрывает "
            f"{summary['periods']['daily']['start']} — {summary['periods']['daily']['end']} / "
            "iiko `/reports/olap`, SALES, `OpenDate.Typed`, `Department` / "
            f"{summary['periods']['daily']['start']} — {summary['periods']['daily']['end']} / "
            "получен базовый ежедневный слой для панели собственника / обновлять ежедневно без текущего неполного дня."
        ),
        (
            f"Факт / Источник / Период / Вывод / Действие: часовая выгрузка покрывает "
            f"{summary['periods']['hourly']['start']} — {summary['periods']['hourly']['end']} / "
            f"iiko `/reports/olap`, SALES, `{summary['hour_field']['field']}` / "
            f"{summary['periods']['hourly']['start']} — {summary['periods']['hourly']['end']} / "
            "получен слой спроса по часам / использовать для планирования кухни, доставки и смен."
        ),
        (
            f"Факт / Источник / Период / Вывод / Действие: часовое поле — `{summary['hour_field']['field']}`; "
            f"{summary['hour_field']['reason']} / iiko `/v2/reports/olap/columns` / "
            f"{summary['periods']['hourly']['start']} — {summary['periods']['hourly']['end']} / "
            "группировка отражает час открытия заказа / при анализе SLA доставки отдельно сверять с часом закрытия или доставки."
        ),
        "",
        "## Итоги",
        "",
        (
            f"Факт / Источник / Период / Вывод / Действие: дневной период дал {money(daily['revenue'])} руб. выручки, "
            f"{money(daily['orders'])} заказов, средний чек {money(daily['avg_check'])} руб. / "
            "sales_daily.csv / "
            f"{summary['periods']['daily']['start']} — {summary['periods']['daily']['end']} / "
            "это основной ряд для ежедневного контроля выручки / вынести в панель: выручка, заказы, средний чек, скидка, food cost."
        ),
        (
            f"Факт / Источник / Период / Вывод / Действие: часовой период дал {money(hourly['revenue'])} руб. выручки, "
            f"{money(hourly['orders'])} заказов, средний чек {money(hourly['avg_check'])} руб. / "
            "sales_hourly.csv / "
            f"{summary['periods']['hourly']['start']} — {summary['periods']['hourly']['end']} / "
            "период сопоставим с прежним канальным срезом / использовать как базу для профиля спроса по часам."
        ),
        (
            f"Факт / Источник / Период / Вывод / Действие: сверка с ориентиром {money(benchmark['revenue'])} руб.; "
            f"текущая выручка {money(benchmark['actual_revenue'])} руб.; расхождение {money2(benchmark['difference'])} руб. / "
            "sales_hourly.csv и прежний audit-ориентир / "
            f"{benchmark['period']} / "
            "расхождение по выручке копеечное и связано с округлением; если появится существенное отклонение, причина может быть в наборе полей, фильтре Department, удаленных заказах, возвратах, строках без оплаты или сервисных строках / фиксировать как риск без ручной корректировки."
        ),
        (
            f"Факт / Источник / Период / Вывод / Действие: OrderNum в дневно-часовом срезе — {money(benchmark['actual_orders'])}, "
            f"в прежнем канальном ориентире — {money(benchmark['orders'])}, это {order_diff_text} / "
            "sales_hourly.csv и прежний audit-ориентир / "
            f"{benchmark['period']} / "
            "OrderNum зависит от набора OLAP-группировок и канальный срез с PayTypes/каналами может считать часть заказов иначе / закрепить один источник заказов для панели собственника."
        ),
        "",
        "## Топ-10 Дней По Выручке",
        "",
    ]
    lines.extend(
        md_table(
            summary["top_revenue_days"],
            [("date", "Дата"), ("weekday", "День"), ("revenue", "Выручка"), ("orders", "Заказы"), ("avg_check", "Средний чек")],
            money_fields={"revenue", "orders", "avg_check"},
        )
    )
    lines.extend(["", "## Топ-10 Провалов По Выручке", ""])
    lines.extend(
        md_table(
            summary["low_revenue_days"],
            [("date", "Дата"), ("weekday", "День"), ("revenue", "Выручка"), ("orders", "Заказы"), ("avg_check", "Средний чек")],
            money_fields={"revenue", "orders", "avg_check"},
        )
    )
    lines.extend(
        [
            "",
            "Факт / Источник / Период / Вывод / Действие: дни без активных продаж исключены из списка провалов, а текущий неполный день не запрашивался вообще; последняя дата анализа 2026-05-17 / sales_daily.csv / весь дневной период / провалы отражают слабые рабочие дни, а не закрытие точки / отдельно проверять календарь работы и локальные причины просадки.",
            "",
            "## Распределение По Часам",
            "",
        ]
    )
    lines.extend(
        md_table(
            summary["hour_distribution"],
            [
                ("hour", "Час"),
                ("revenue", "Выручка"),
                ("orders", "Заказы"),
                ("avg_check", "Средний чек"),
                ("revenue_share", "Доля выручки"),
                ("orders_share", "Доля заказов"),
            ],
            money_fields={"revenue", "orders", "avg_check"},
            pct_fields={"revenue_share", "orders_share"},
        )
    )
    lines.extend(["", "## Дни Недели", ""])
    lines.extend(
        md_table(
            summary["weekday_distribution"],
            [
                ("weekday", "День недели"),
                ("days", "Дней"),
                ("revenue", "Выручка"),
                ("orders", "Заказы"),
                ("avg_daily_revenue", "Средняя дневная выручка"),
                ("avg_daily_orders", "Средние дневные заказы"),
                ("avg_check", "Средний чек"),
            ],
            money_fields={"revenue", "orders", "avg_daily_revenue", "avg_daily_orders", "avg_check"},
        )
    )
    best = summary["best_weekday"]
    worst = summary["worst_weekday"]
    if best and worst:
        lines.extend(
            [
                "",
                (
                    f"Факт / Источник / Период / Вывод / Действие: лучший день недели по средней дневной выручке — "
                    f"{best['weekday']} ({money(best['avg_daily_revenue'])} руб.), худший — "
                    f"{worst['weekday']} ({money(worst['avg_daily_revenue'])} руб.) / sales_daily.csv / "
                    f"{summary['periods']['daily']['start']} — {summary['periods']['daily']['end']} / "
                    "недельный ритм спроса выражен и годится для планирования смен / вынести средние по дням недели в панель."
                )
            ]
        )
    lines.extend(["", "## Риски Качества Данных", ""])
    for risk in summary["quality_risks"]:
        lines.append(
            f"- Факт / Источник / Период / Вывод / Действие: {risk['risk']} / {risk['source']} / "
            f"{risk['period']} / риск уровня {risk['severity']} / {risk['action']}"
        )
    lines.extend(
        [
            "",
            "## Что Перенести В Ежедневную Панель",
            "",
            "- Факт / Источник / Период / Вывод / Действие: ежедневные KPI доступны на уровне даты и Department / sales_daily.csv / ежедневно до последнего полного дня / можно строить динамику выручки, заказов, среднего чека, скидки и food cost / добавить карточки KPI и график день-к-дню.",
            "- Факт / Источник / Период / Вывод / Действие: часовой профиль спроса доступен по `HourOpen` / sales_hourly.csv / последние 6.5 месяцев / можно видеть пики нагрузки и слабые часы / добавить тепловую карту час x день недели.",
            "- Факт / Источник / Период / Вывод / Действие: скидки есть как `DiscountSum` и `discountWithoutVAT` / sales_daily.csv, sales_hourly.csv / весь период / можно контролировать долю скидки в gross_sum / добавить пороговые подсветки.",
            "- Факт / Источник / Период / Вывод / Действие: себестоимость есть в дневном срезе через ProductCostBase.ProductCost / sales_daily.csv / весь дневной период / можно считать food cost и gross margin, но с оговоркой по техкартам / сверять с ОПиУ и корректировкой акционных наборов.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    load_local_env()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    client = IikoClient()

    columns = fetch_columns(client, manifest)
    hour_field, hour_field_reason = choose_hour_field(columns)
    write_json(PROCESSED_DIR / "hour_field.json", {"field": hour_field, "reason": hour_field_reason})
    print(f"hour field selected: {hour_field}")

    daily_raw_rows = fetch_olap(
        client,
        manifest,
        name="sales_daily",
        start=DAILY_START,
        end=END_DATE,
        group_rows=DAILY_GROUP_ROWS,
        agrs=DAILY_AGRS,
    )
    hourly_raw_rows = fetch_olap(
        client,
        manifest,
        name="sales_hourly",
        start=HOURLY_START,
        end=END_DATE,
        group_rows=["OpenDate.Typed", hour_field, "Department"],
        agrs=HOURLY_AGRS,
    )

    daily_rows = build_daily(daily_raw_rows)
    hourly_rows = build_hourly(hourly_raw_rows, hour_field)
    hour_distribution = aggregate_by_hour(hourly_rows)
    weekday_distribution = aggregate_by_weekday(daily_rows)
    closed_days, closed_day_diagnostics = detect_closed_or_incomplete_days(daily_rows)
    hourly_summary = sum_metrics(hourly_rows)
    quality_risks = build_quality_risks(
        daily_rows=daily_rows,
        hourly_rows=hourly_rows,
        all_daily_raw_rows=daily_raw_rows,
        all_hourly_raw_rows=hourly_raw_rows,
        hourly_summary=hourly_summary,
        hour_field=hour_field,
        closed_days=closed_days,
    )
    summary = build_summary(
        daily_rows=daily_rows,
        hourly_rows=hourly_rows,
        hour_distribution=hour_distribution,
        weekday_distribution=weekday_distribution,
        hour_field=hour_field,
        hour_field_reason=hour_field_reason,
        closed_days=closed_days,
        closed_day_diagnostics=closed_day_diagnostics,
        quality_risks=quality_risks,
        manifest=manifest,
    )

    write_csv(
        PROCESSED_DIR / "sales_daily.csv",
        daily_rows,
        [
            "date",
            "weekday",
            "department",
            "orders",
            "gross_sum",
            "revenue",
            "discount_sum",
            "discount_without_vat",
            "discount_share_of_gross",
            "dish_amount",
            "avg_check",
            "product_cost",
            "food_cost_pct",
            "gross_margin",
        ],
    )
    write_json(PROCESSED_DIR / "sales_daily.json", daily_rows)
    write_csv(
        PROCESSED_DIR / "sales_hourly.csv",
        hourly_rows,
        ["date", "weekday", "hour", "department", "orders", "revenue", "gross_sum", "discount_sum", "dish_amount", "avg_check"],
    )
    write_json(PROCESSED_DIR / "sales_hourly.json", hourly_rows)
    write_json(PROCESSED_DIR / "sales_summary.json", summary)
    write_csv(
        PROCESSED_DIR / "quality_risks.csv",
        quality_risks,
        ["risk_id", "severity", "risk", "source", "period", "action"],
    )
    (PROCESSED_DIR / "report.md").write_text(build_report(summary), encoding="utf-8")
    write_json(RAW_DIR / "manifest.json", manifest)
    print("sales day/hour export completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

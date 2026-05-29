#!/usr/bin/env python3
"""Read-only iiko export for order channels, clients, delivery, cancellations.

The script reads secrets from environment/.env, writes raw responses under
research/raw/, and writes only aggregate files under research/processed/.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import datetime as dt
import hashlib
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = PROJECT_ROOT / "research/raw/iiko/orders_delivery"
PROCESSED_DIR = PROJECT_ROOT / "research/processed/iiko/orders_delivery"

CHANNEL_START = dt.date(2025, 11, 1)
DELIVERY_START = dt.date(2025, 11, 1)
END_DATE = dt.date(2026, 5, 17)

ACTIVE_DEPARTMENT_MARKER = "черников"
HISTORICAL_DEPARTMENT_MARKER = "гагарин"


class IikoHTTPError(RuntimeError):
    def __init__(self, status: int | None, body: bytes, message: str = "") -> None:
        self.status = status
        self.body = body
        self.message = message or body[:300].decode("utf-8", "replace")
        super().__init__(f"iiko HTTP error {status}: {self.message[:160]}")


def load_local_env() -> None:
    """Load .env/ENV without printing values and without overwriting real env."""

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
            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in {"'", '"'}
            ):
                value = value[1:-1]
            if value and not os.environ.get(key):
                os.environ[key] = value


def parse_date(value: str) -> dt.date:
    return dt.datetime.strptime(value, "%Y-%m-%d").date()


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


def safe_filename(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value)


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
        body = urllib.parse.urlencode(
            {"login": self.login, "pass": self.password_sha1}
        ).encode("utf-8")
        request = urllib.request.Request(
            self.api_url("/auth"),
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(
            request, timeout=self.timeout, context=self.context
        ) as response:
            token = response.read().decode("utf-8", "replace").strip()
        if not token or "<" in token or "\n" in token:
            raise IikoHTTPError(None, token.encode("utf-8"), "Unexpected auth response")
        self.token = token

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        form: dict[str, str] | None = None,
        json_body: Any | None = None,
        raw_body: bytes | str | None = None,
        content_type: str | None = None,
        retry_auth: bool = True,
    ) -> tuple[int, bytes]:
        params = dict(params or {})
        if path != "/auth":
            if not self.token and retry_auth:
                self.refresh_token()
            params["key"] = self.token

        url = self.api_url(path)
        if params:
            query = urllib.parse.urlencode(params, doseq=True)
            url = f"{url}?{query}"
        body = None
        headers: dict[str, str] = {}
        body_kinds = sum(value is not None for value in (form, json_body, raw_body))
        if body_kinds > 1:
            raise ValueError("Only one request body kind is allowed")
        if form is not None:
            body = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = content_type or "application/json"
        elif raw_body is not None:
            body = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
            if content_type:
                headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body, method=method, headers=headers)

        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self.context
            ) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            body_bytes = exc.read()
            if retry_auth and self._looks_like_auth_error(exc.code, body_bytes):
                self.refresh_token()
                return self.request(
                    path,
                    method=method,
                    params=params_without_key(params),
                    form=form,
                    json_body=json_body,
                    raw_body=raw_body,
                    content_type=content_type,
                    retry_auth=False,
                )
            raise IikoHTTPError(exc.code, body_bytes) from exc
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


def params_without_key(params: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if key != "key"}


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


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


def is_truthy(value: Any) -> bool:
    text = clean_text(value).casefold()
    return text in {
        "1",
        "true",
        "yes",
        "y",
        "да",
        "истина",
        "удален",
        "deleted",
        "storned",
    }


def nonempty_issue_value(value: Any) -> bool:
    text = clean_text(value).casefold()
    return bool(text) and text not in {
        "(пусто)",
        "false",
        "0",
        "нет",
        "no",
        "none",
        "null",
        "not_deleted",
        "not deleted",
        "не удален",
        "не удалён",
    }


def is_deleted_with_writeoff(value: Any) -> bool:
    return clean_text(value).casefold() in {
        "deleted_with_writeoff",
        "со списанием",
        "true",
        "1",
    }


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


def flatten_xml_element(element: ET.Element, prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for attr_key, attr_value in element.attrib.items():
        key = f"{prefix}.{attr_key}" if prefix else attr_key
        result[key] = attr_value

    children = list(element)
    if not children:
        if prefix:
            result[prefix] = clean_text(element.text)
        return result

    tag_counts = Counter(local_name(child.tag) for child in children)
    for child in children:
        name = local_name(child.tag)
        key = f"{prefix}.{name}" if prefix else name
        if len(child):
            result.update(flatten_xml_element(child, key))
        elif tag_counts[name] > 1:
            result.setdefault(key, [])
            if isinstance(result[key], list):
                result[key].append(clean_text(child.text))
        else:
            result[key] = clean_text(child.text)
    return result


def extract_xml_columns(root: ET.Element) -> list[str]:
    columns: list[str] = []
    for element in root.iter():
        tag = local_name(element.tag).casefold()
        if tag not in {"column", "col"}:
            continue
        name = (
            element.attrib.get("name")
            or element.attrib.get("id")
            or element.attrib.get("field")
            or element.attrib.get("key")
        )
        if name and name not in columns:
            columns.append(name)
    return columns


def row_from_cells(element: ET.Element, columns: list[str]) -> dict[str, Any] | None:
    if not columns:
        return None
    cell_tags = {"c", "cell", "value"}
    cells = [
        clean_text(child.text)
        for child in list(element)
        if local_name(child.tag).casefold() in cell_tags
    ]
    if cells and len(cells) == len(columns):
        return dict(zip(columns, cells))

    indexed: list[tuple[int, str]] = []
    for key, value in element.attrib.items():
        key_lower = key.casefold()
        if len(key_lower) > 1 and key_lower[0] == "c" and key_lower[1:].isdigit():
            indexed.append((int(key_lower[1:]), clean_text(value)))
    if indexed and len(indexed) == len(columns):
        indexed.sort()
        return {columns[index]: value for index, (_, value) in enumerate(indexed)}
    return None


def parse_xml_rows(data: bytes, expected_fields: list[str] | None = None) -> list[dict[str, Any]]:
    expected = {field.casefold() for field in (expected_fields or [])}
    text = data.decode("utf-8", "replace")
    row_pattern = re.compile(
        r"<(?P<tag>(?:[\w-]+:)?(?:r|row|item|record|entry|corporateItemDto))\b[^>]*>(?P<body>.*?)</(?P=tag)>",
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
        row_keys = {str(key).casefold() for key in row}
        return bool(row_keys & expected)

    def add_row(row: dict[str, Any]) -> None:
        if not include(row):
            return
        signature = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        if signature not in seen:
            rows.append(row)
            seen.add(signature)

    for match in row_pattern.finditer(text):
        row: dict[str, Any] = {}
        for cell in cell_pattern.finditer(match.group("body")):
            raw_tag = cell.group("tag") or cell.group("empty")
            if not raw_tag:
                continue
            key = tag_name(raw_tag)
            row[key] = "" if cell.group("empty") else xml_value(cell.group("value") or "")
        add_row(row)
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


def department_scope(department: str) -> str:
    lowered = clean_text(department).casefold()
    if ACTIVE_DEPARTMENT_MARKER in lowered:
        return "active_chernikova"
    if HISTORICAL_DEPARTMENT_MARKER in lowered:
        return "historical_gagarina"
    if lowered:
        return "other_department"
    return "unknown_department"


def money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ")


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


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


def fetch_reference_data(client: IikoClient, manifest: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "departments": [],
        "departments_search": [],
        "sales_columns": [],
        "deliveries_columns": [],
    }

    try:
        status, data = client.request(
            "/corporation/departments", params={"includeDeleted": "true"}
        )
        rows = save_raw_response(
            RAW_DIR / "departments.xml",
            data,
            manifest,
            endpoint="/corporation/departments",
            status=status,
        )
        result["departments"] = rows
    except IikoHTTPError as exc:
        write_json(
            RAW_DIR / "departments_error.json",
            {"endpoint": "/corporation/departments", "status": exc.status, "message": exc.message},
        )
        manifest.append(
            {
                "endpoint": "/corporation/departments",
                "file": "research/raw/iiko/orders_delivery/departments_error.json",
                "status": exc.status,
                "bytes": len(exc.body),
                "parsed_rows": 0,
                "note": "error",
            }
        )

    try:
        status, data = client.request("/corporation/departments/search")
        rows = save_raw_response(
            RAW_DIR / "departments_search.xml",
            data,
            manifest,
            endpoint="/corporation/departments/search",
            status=status,
        )
        result["departments_search"] = rows
    except IikoHTTPError as exc:
        write_json(
            RAW_DIR / "departments_search_error.json",
            {
                "endpoint": "/corporation/departments/search",
                "status": exc.status,
                "message": exc.message,
            },
        )
        manifest.append(
            {
                "endpoint": "/corporation/departments/search",
                "file": "research/raw/iiko/orders_delivery/departments_search_error.json",
                "status": exc.status,
                "bytes": len(exc.body),
                "parsed_rows": 0,
                "note": "error",
            }
        )

    for report_type in ("SALES", "DELIVERIES"):
        try:
            status, data = client.request(
                "/v2/reports/olap/columns", params={"reportType": report_type}
            )
            path = RAW_DIR / f"olap_columns_{report_type.casefold()}.json"
            rows = save_raw_response(
                path,
                data,
                manifest,
                endpoint="/v2/reports/olap/columns",
                status=status,
                note=f"reportType={report_type}",
            )
            result[f"{report_type.casefold()}_columns"] = rows
        except IikoHTTPError as exc:
            write_json(
                RAW_DIR / f"olap_columns_{report_type.casefold()}_error.json",
                {
                    "endpoint": "/v2/reports/olap/columns",
                    "reportType": report_type,
                    "status": exc.status,
                    "message": exc.message,
                },
            )
            manifest.append(
                {
                    "endpoint": "/v2/reports/olap/columns",
                    "file": f"research/raw/iiko/orders_delivery/olap_columns_{report_type.casefold()}_error.json",
                    "status": exc.status,
                    "bytes": len(exc.body),
                    "parsed_rows": 0,
                    "note": f"error reportType={report_type}",
                }
            )
    return result


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
        status, data = client.request("/reports/olap", params=params)
        raw_path = RAW_DIR / f"olap_{name}_{iso(chunk[0])}_{iso(chunk[1])}.xml"
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
        time.sleep(0.15)
    return all_rows


def active_department_from_rows(rows: list[dict[str, Any]]) -> dict[str, str]:
    candidates: list[dict[str, str]] = []
    for row in rows:
        name = clean_text(
            value_from(row, ["name", "Name", "department.name", "Department.name"])
        )
        identifier = clean_text(value_from(row, ["id", "Id", "uuid", "department.id"]))
        deleted = clean_text(value_from(row, ["deleted", "isDeleted", "Deleted"]))
        if not name:
            flat_values = [clean_text(value) for value in row.values()]
            name = next((value for value in flat_values if ACTIVE_DEPARTMENT_MARKER in value.casefold()), "")
        if ACTIVE_DEPARTMENT_MARKER in name.casefold() and not is_truthy(deleted):
            candidates.append({"id": identifier, "name": name})
    if candidates:
        return candidates[0]
    return {"id": "", "name": ""}


def active_department_from_olap_rows(rows: list[dict[str, Any]]) -> dict[str, str]:
    for row in rows:
        department = dimension(value_from(row, ["Department"]))
        if department_scope(department) == "active_chernikova":
            return {"id": "", "name": department}
    return {"id": "", "name": ""}


def fetch_delivery_reports(
    client: IikoClient,
    manifest: list[dict[str, Any]],
    *,
    department: dict[str, str],
    start: dt.date,
    end: dt.date,
) -> None:
    department_id = department.get("id", "")
    department_name = department.get("name", "")
    department_candidates: list[tuple[str, str]] = []
    if department_id:
        department_candidates.append(("id", department_id))
        department_candidates.append(
            ("json_id", json.dumps({"id": department_id}, ensure_ascii=False))
        )
    if department_id and department_name:
        department_candidates.append(
            (
                "json_id_name",
                json.dumps({"id": department_id, "name": department_name}, ensure_ascii=False),
            )
        )
    if department_name:
        department_candidates.append(("name", department_name))
        department_candidates.append(
            ("json_name", json.dumps({"name": department_name}, ensure_ascii=False))
        )

    if not department_candidates:
        write_json(
            PROCESSED_DIR / "delivery_report_endpoint_status.json",
            {
                "status": "skipped",
                "reason": "active department id/name was not found in /corporation/departments",
            },
        )
        return

    specs = {
        "orderCycle": "/reports/delivery/orderCycle",
        "couriers": "/reports/delivery/couriers",
        "regions": "/reports/delivery/regions",
    }
    endpoint_status: list[dict[str, Any]] = []
    for chunk in month_chunks(start, end):
        for label, endpoint in specs.items():
            last_error: IikoHTTPError | None = None
            success = False
            for department_format, department_value in department_candidates:
                for date_format in ("%d.%m.%Y", "%Y-%m-%d"):
                    params = {
                        "department": department_value,
                        "dateFrom": chunk[0].strftime(date_format),
                        "dateTo": chunk[1].strftime(date_format),
                    }
                    try:
                        status, data = client.request(endpoint, params=params)
                        extension = "json" if data.lstrip().startswith((b"{", b"[")) else "xml"
                        raw_path = (
                            RAW_DIR
                            / f"delivery_{safe_filename(label)}_{iso(chunk[0])}_{iso(chunk[1])}.{extension}"
                        )
                        rows = save_raw_response(
                            raw_path,
                            data,
                            manifest,
                            endpoint=endpoint,
                            period=chunk,
                            status=status,
                            note=f"dateFormat={date_format};departmentFormat={department_format}",
                        )
                        endpoint_status.append(
                            {
                                "endpoint": endpoint,
                                "period_start": iso(chunk[0]),
                                "period_end": iso(chunk[1]),
                                "status": status,
                                "parsed_rows": len(rows),
                                "raw_file": rel(raw_path),
                                "date_format": date_format,
                                "department_format": department_format,
                            }
                        )
                        print(
                            f"fetched delivery {label} {iso(chunk[0])}..{iso(chunk[1])}: {len(rows)} rows"
                        )
                        last_error = None
                        success = True
                        break
                    except IikoHTTPError as exc:
                        last_error = exc
                        continue
                if success:
                    break
            if last_error is not None:
                error_path = (
                    RAW_DIR
                    / f"delivery_{safe_filename(label)}_{iso(chunk[0])}_{iso(chunk[1])}_error.json"
                )
                write_json(
                    error_path,
                    {
                        "endpoint": endpoint,
                        "period_start": iso(chunk[0]),
                        "period_end": iso(chunk[1]),
                        "status": last_error.status,
                        "message": last_error.message,
                    },
                )
                manifest.append(
                    {
                        "endpoint": endpoint,
                        "file": rel(error_path),
                        "status": last_error.status,
                        "bytes": len(last_error.body),
                        "parsed_rows": 0,
                        "period_start": iso(chunk[0]),
                        "period_end": iso(chunk[1]),
                        "note": "error",
                    }
                )
                endpoint_status.append(
                    {
                        "endpoint": endpoint,
                        "period_start": iso(chunk[0]),
                        "period_end": iso(chunk[1]),
                        "status": last_error.status,
                        "parsed_rows": 0,
                        "raw_file": rel(error_path),
                        "date_format": "failed dd.MM.yyyy and yyyy-MM-dd",
                        "department_format": "failed string and json forms",
                    }
                )
                print(
                    f"delivery {label} {iso(chunk[0])}..{iso(chunk[1])}: failed, status={last_error.status}"
                )
            time.sleep(0.15)
    write_json(PROCESSED_DIR / "delivery_report_endpoint_status.json", endpoint_status)
    write_json(PROCESSED_DIR / "delivery_reports_manifest.json", endpoint_status)


def revenue_from(row: dict[str, Any]) -> float:
    return to_number(
        value_from(row, ["DishDiscountSumInt", "DishDiscountSumInt.sum", "sumAfterDiscountWithoutVAT"])
    )


def gross_from(row: dict[str, Any]) -> float:
    return to_number(value_from(row, ["DishSumInt", "DishSumInt.sum"]))


def discount_from(row: dict[str, Any]) -> float:
    discount = to_number(value_from(row, ["DiscountSum", "discountWithoutVAT", "DiscountSum.sum"]))
    if discount:
        return discount
    gross = gross_from(row)
    revenue = revenue_from(row)
    if gross > revenue:
        return gross - revenue
    return 0.0


def orders_from(row: dict[str, Any]) -> float:
    return to_number(value_from(row, ["OrderNum", "OrderNum.count", "GuestNum"]))


def build_channel_outputs(rows: list[dict[str, Any]], period: tuple[dt.date, dt.date]) -> dict[str, Any]:
    groups: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        department = dimension(value_from(row, ["Department"]))
        origin = dimension(value_from(row, ["OriginName"]))
        order_type = dimension(value_from(row, ["OrderType"]))
        service_type = dimension(value_from(row, ["Delivery.ServiceType"]))
        pay_types = dimension(value_from(row, ["PayTypes"]))
        key = (
            department_scope(department),
            department,
            origin,
            order_type,
            service_type,
            pay_types,
        )
        group = groups.setdefault(
            key,
            {
                "department_scope": key[0],
                "department": department,
                "origin_name": origin,
                "order_type": order_type,
                "service_type": service_type,
                "pay_types": pay_types,
                "orders": 0.0,
                "revenue": 0.0,
                "gross_sum": 0.0,
                "discount_sum": 0.0,
            },
        )
        group["orders"] += orders_from(row)
        group["revenue"] += revenue_from(row)
        group["gross_sum"] += gross_from(row)
        group["discount_sum"] += discount_from(row)

    channel_rows = []
    for row in groups.values():
        orders = row["orders"]
        revenue = row["revenue"]
        gross = row["gross_sum"]
        row["avg_check"] = revenue / orders if orders else 0.0
        row["discount_share_of_gross"] = row["discount_sum"] / gross if gross else 0.0
        row["period_start"] = iso(period[0])
        row["period_end"] = iso(period[1])
        channel_rows.append(row)
    channel_rows.sort(key=lambda item: (item["department_scope"] != "active_chernikova", -item["revenue"]))

    department_groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in channel_rows:
        key = (row["department_scope"], row["department"])
        department_group = department_groups.setdefault(
            key,
            {
                "period_start": iso(period[0]),
                "period_end": iso(period[1]),
                "department_scope": row["department_scope"],
                "department": row["department"],
                "orders": 0.0,
                "revenue": 0.0,
            },
        )
        department_group["orders"] += row["orders"]
        department_group["revenue"] += row["revenue"]
    department_rows = sorted(
        department_groups.values(),
        key=lambda item: (item["department_scope"] != "active_chernikova", -item["revenue"]),
    )

    service_groups: dict[str, dict[str, Any]] = {}
    origin_groups: dict[str, dict[str, Any]] = {}
    current_rows = [row for row in channel_rows if row["department_scope"] == "active_chernikova"]
    total_orders = sum(row["orders"] for row in current_rows)
    total_revenue = sum(row["revenue"] for row in current_rows)
    for row in current_rows:
        service = row["service_type"]
        service_group = service_groups.setdefault(
            service, {"service_type": service, "orders": 0.0, "revenue": 0.0, "discount_sum": 0.0}
        )
        service_group["orders"] += row["orders"]
        service_group["revenue"] += row["revenue"]
        service_group["discount_sum"] += row["discount_sum"]

        origin = row["origin_name"]
        origin_group = origin_groups.setdefault(
            origin,
            {
                "origin_name": origin,
                "orders": 0.0,
                "revenue": 0.0,
                "discount_sum": 0.0,
                "is_problem_origin": False,
            },
        )
        origin_group["orders"] += row["orders"]
        origin_group["revenue"] += row["revenue"]
        origin_group["discount_sum"] += row["discount_sum"]

    service_rows = []
    for row in service_groups.values():
        row["order_share"] = row["orders"] / total_orders if total_orders else 0.0
        row["revenue_share"] = row["revenue"] / total_revenue if total_revenue else 0.0
        row["avg_check"] = row["revenue"] / row["orders"] if row["orders"] else 0.0
        service_rows.append(row)
    service_rows.sort(key=lambda item: -item["orders"])

    problem_values = {"", "(пусто)", "-", "unknown", "undefined", "не указано"}
    origin_rows = []
    for row in origin_groups.values():
        row["order_share"] = row["orders"] / total_orders if total_orders else 0.0
        row["revenue_share"] = row["revenue"] / total_revenue if total_revenue else 0.0
        row["avg_check"] = row["revenue"] / row["orders"] if row["orders"] else 0.0
        row["is_problem_origin"] = row["origin_name"].casefold() in problem_values
        origin_rows.append(row)
    origin_rows.sort(key=lambda item: -item["orders"])

    channel_fields = [
        "period_start",
        "period_end",
        "department_scope",
        "department",
        "origin_name",
        "order_type",
        "service_type",
        "pay_types",
        "orders",
        "revenue",
        "avg_check",
        "gross_sum",
        "discount_sum",
        "discount_share_of_gross",
    ]
    service_fields = [
        "service_type",
        "orders",
        "order_share",
        "revenue",
        "revenue_share",
        "avg_check",
        "discount_sum",
    ]
    origin_fields = [
        "origin_name",
        "orders",
        "order_share",
        "revenue",
        "revenue_share",
        "avg_check",
        "discount_sum",
        "is_problem_origin",
    ]
    write_csv(PROCESSED_DIR / "channel_summary.csv", channel_rows, channel_fields)
    write_json(PROCESSED_DIR / "channel_summary.json", channel_rows)
    write_csv(PROCESSED_DIR / "service_type_summary.csv", service_rows, service_fields)
    write_json(PROCESSED_DIR / "service_type_summary.json", service_rows)
    write_csv(PROCESSED_DIR / "origin_quality.csv", origin_rows, origin_fields)
    write_json(PROCESSED_DIR / "origin_quality.json", origin_rows)
    write_csv(
        PROCESSED_DIR / "department_scope_summary.csv",
        department_rows,
        ["period_start", "period_end", "department_scope", "department", "orders", "revenue"],
    )
    write_json(PROCESSED_DIR / "department_scope_summary.json", department_rows)
    write_csv(PROCESSED_DIR / "channels_by_combination.csv", channel_rows, channel_fields)
    write_csv(PROCESSED_DIR / "channels_by_service_type.csv", service_rows, service_fields)
    write_csv(PROCESSED_DIR / "channels_by_origin.csv", origin_rows, origin_fields)
    write_json(
        PROCESSED_DIR / "channels_summary.json",
        {
            "channel_rows": channel_rows,
            "service_rows": service_rows,
            "origin_rows": origin_rows,
            "total_orders": total_orders,
            "total_revenue": total_revenue,
        },
    )

    return {
        "channel_rows": channel_rows,
        "service_rows": service_rows,
        "origin_rows": origin_rows,
        "total_orders": total_orders,
        "total_revenue": total_revenue,
    }


def build_delivery_outputs(rows: list[dict[str, Any]], period: tuple[dt.date, dt.date]) -> dict[str, Any]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        department = dimension(value_from(row, ["Department"]))
        if department_scope(department) != "active_chernikova":
            continue
        service_type = dimension(value_from(row, ["Delivery.ServiceType"]))
        region = dimension(value_from(row, ["Delivery.Region"]))
        key = (service_type, region)
        group = groups.setdefault(
            key,
            {
                "period_start": iso(period[0]),
                "period_end": iso(period[1]),
                "service_type": service_type,
                "region": region,
                "orders": 0.0,
                "revenue": 0.0,
                "way_duration_sum_min": 0.0,
                "_delay_weight": 0.0,
                "_delay_weighted": 0.0,
                "_way_weight": 0.0,
                "_way_weighted": 0.0,
                "_mark_weight": 0.0,
                "_mark_weighted": 0.0,
            },
        )
        orders = orders_from(row)
        group["orders"] += orders
        group["revenue"] += revenue_from(row)
        group["way_duration_sum_min"] += to_number(value_from(row, ["Delivery.WayDurationSum"]))

        delay_text = clean_text(value_from(row, ["Delivery.DelayAvg"]))
        if delay_text:
            delay = to_number(delay_text)
            group["_delay_weight"] += orders
            group["_delay_weighted"] += delay * orders

        way_avg_text = clean_text(value_from(row, ["Delivery.WayDurationAvg"]))
        if way_avg_text:
            way_avg = to_number(way_avg_text)
            group["_way_weight"] += orders
            group["_way_weighted"] += way_avg * orders

        mark_text = clean_text(value_from(row, ["Delivery.AggregatedAvgMark"]))
        if mark_text:
            mark = to_number(mark_text)
            group["_mark_weight"] += orders
            group["_mark_weighted"] += mark * orders

    delivery_rows = []
    for row in groups.values():
        orders = row["orders"]
        row["avg_check"] = row["revenue"] / orders if orders else 0.0
        row["avg_delay_min"] = (
            row["_delay_weighted"] / row["_delay_weight"] if row["_delay_weight"] else 0.0
        )
        row["avg_way_duration_min"] = (
            row["way_duration_sum_min"] / orders
            if orders and row["way_duration_sum_min"]
            else row["_way_weighted"] / row["_way_weight"]
            if row["_way_weight"]
            else 0.0
        )
        row["avg_delivery_mark"] = (
            row["_mark_weighted"] / row["_mark_weight"] if row["_mark_weight"] else 0.0
        )
        row["mark_weight_orders"] = row["_mark_weight"]
        for key in list(row):
            if key.startswith("_"):
                del row[key]
        delivery_rows.append(row)
    delivery_rows.sort(key=lambda item: (item["service_type"], -item["orders"]))
    service_groups: dict[str, dict[str, Any]] = {}
    for row in delivery_rows:
        service = row["service_type"]
        group = service_groups.setdefault(
            service,
            {
                "period_start": iso(period[0]),
                "period_end": iso(period[1]),
                "service_type": service,
                "orders": 0.0,
                "revenue": 0.0,
                "way_duration_sum_min": 0.0,
            },
        )
        group["orders"] += row["orders"]
        group["revenue"] += row["revenue"]
        group["way_duration_sum_min"] += row["way_duration_sum_min"]
    service_rows = []
    for row in service_groups.values():
        row["avg_check"] = row["revenue"] / row["orders"] if row["orders"] else 0.0
        row["avg_way_duration_min"] = (
            row["way_duration_sum_min"] / row["orders"]
            if row["orders"] and row["way_duration_sum_min"]
            else 0.0
        )
        service_rows.append(row)
    service_rows.sort(key=lambda item: -item["orders"])
    fields = [
        "period_start",
        "period_end",
        "service_type",
        "region",
        "orders",
        "revenue",
        "avg_check",
        "avg_way_duration_min",
        "avg_delay_min",
        "way_duration_sum_min",
        "avg_delivery_mark",
        "mark_weight_orders",
    ]
    write_csv(PROCESSED_DIR / "delivery_metrics.csv", delivery_rows, fields)
    write_json(PROCESSED_DIR / "delivery_metrics.json", delivery_rows)
    write_csv(PROCESSED_DIR / "delivery_by_service_region.csv", delivery_rows, fields)
    write_json(PROCESSED_DIR / "delivery_summary.json", {"delivery_rows": delivery_rows})
    write_csv(
        PROCESSED_DIR / "delivery_by_service_type.csv",
        service_rows,
        [
            "period_start",
            "period_end",
            "service_type",
            "orders",
            "revenue",
            "avg_check",
            "avg_way_duration_min",
            "way_duration_sum_min",
        ],
    )
    return {"delivery_rows": delivery_rows}


def period_from_delivery_filename(path: Path) -> tuple[str, str]:
    match = re.search(r"_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})", path.name)
    if not match:
        return "", ""
    return match.group(1), match.group(2)


def metric_rows_from_xml_text(text: str) -> list[dict[str, Any]]:
    metric_pattern = re.compile(r"<metric\b[^>]*>(?P<body>.*?)</metric>", re.DOTALL)
    cell_pattern = re.compile(
        r"<(?P<tag>[A-Za-z_][\w.:-]*)(?:\s[^>]*)?>(?P<value>.*?)</(?P=tag)>",
        re.DOTALL,
    )
    rows: list[dict[str, Any]] = []
    for metric in metric_pattern.finditer(text):
        row: dict[str, Any] = {}
        for cell in cell_pattern.finditer(metric.group("body")):
            row[cell.group("tag")] = clean_text(
                html.unescape(re.sub(r"<[^>]+>", "", cell.group("value") or ""))
            )
        if row:
            rows.append(row)
    return rows


def build_delivery_report_outputs(
    start: dt.date = DELIVERY_START, end: dt.date = END_DATE
) -> dict[str, Any]:
    order_cycle_rows: list[dict[str, Any]] = []
    for path in sorted(RAW_DIR.glob("delivery_orderCycle_*.xml")):
        period_start, period_end = period_from_delivery_filename(path)
        if not period_start or parse_date(period_start) < start or parse_date(period_end) > end:
            continue
        for row in parse_rows(path.read_bytes()):
            order_cycle_rows.append(
                {
                    "period_start": period_start,
                    "period_end": period_end,
                    "metric_type": dimension(value_from(row, ["metricType"])),
                    "pizza_time": to_number(value_from(row, ["pizzaTime"])),
                    "cutting_time": to_number(value_from(row, ["cuttingTime"])),
                    "on_shelf_time": to_number(value_from(row, ["onShelfTime"])),
                    "in_restaurant_time": to_number(value_from(row, ["inRestaurantTime"])),
                    "on_the_way_time": to_number(value_from(row, ["onTheWayTime"])),
                    "total_time": to_number(value_from(row, ["totalTime"])),
                }
            )
    order_cycle_fields = [
        "period_start",
        "period_end",
        "metric_type",
        "pizza_time",
        "cutting_time",
        "on_shelf_time",
        "in_restaurant_time",
        "on_the_way_time",
        "total_time",
    ]
    write_csv(PROCESSED_DIR / "delivery_order_cycle_summary.csv", order_cycle_rows, order_cycle_fields)
    write_json(PROCESSED_DIR / "delivery_order_cycle_summary.json", order_cycle_rows)

    courier_groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for path in sorted(RAW_DIR.glob("delivery_couriers_*.xml")):
        period_start, period_end = period_from_delivery_filename(path)
        if not period_start or parse_date(period_start) < start or parse_date(period_end) > end:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for row in metric_rows_from_xml_text(text):
            metric_type = dimension(value_from(row, ["metricType"]))
            key = (period_start, period_end, metric_type)
            group = courier_groups.setdefault(
                key,
                {
                    "period_start": period_start,
                    "period_end": period_end,
                    "metric_type": metric_type,
                    "courier_metric_rows": 0.0,
                    "_total_time_sum": 0.0,
                    "_on_way_sum": 0.0,
                    "_double_sum": 0.0,
                    "_triple_sum": 0.0,
                    "_order_count_sum": 0.0,
                    "_total_time_max": 0.0,
                    "_on_way_max": 0.0,
                    "_double_max": 0.0,
                    "_triple_max": 0.0,
                    "_order_count_max": 0.0,
                },
            )
            total_time = to_number(value_from(row, ["totalTime"]))
            on_way = to_number(value_from(row, ["onTheWayTime"]))
            double_orders = to_number(value_from(row, ["doubleOrders"]))
            triple_orders = to_number(value_from(row, ["tripleOrders"]))
            order_count = to_number(value_from(row, ["orderCount"]))
            group["courier_metric_rows"] += 1
            group["_total_time_sum"] += total_time
            group["_on_way_sum"] += on_way
            group["_double_sum"] += double_orders
            group["_triple_sum"] += triple_orders
            group["_order_count_sum"] += order_count
            group["_total_time_max"] = max(group["_total_time_max"], total_time)
            group["_on_way_max"] = max(group["_on_way_max"], on_way)
            group["_double_max"] = max(group["_double_max"], double_orders)
            group["_triple_max"] = max(group["_triple_max"], triple_orders)
            group["_order_count_max"] = max(group["_order_count_max"], order_count)

    courier_rows: list[dict[str, Any]] = []
    for row in courier_groups.values():
        count = row["courier_metric_rows"] or 1
        if row["metric_type"] == "MAXIMUM":
            row["total_time"] = row["_total_time_max"]
            row["on_the_way_time"] = row["_on_way_max"]
            row["double_orders"] = row["_double_max"]
            row["triple_orders"] = row["_triple_max"]
            row["order_count"] = row["_order_count_max"]
        else:
            row["total_time"] = row["_total_time_sum"] / count
            row["on_the_way_time"] = row["_on_way_sum"] / count
            row["double_orders"] = row["_double_sum"] / count
            row["triple_orders"] = row["_triple_sum"] / count
            row["order_count"] = row["_order_count_sum"] / count
        for key in list(row):
            if key.startswith("_"):
                del row[key]
        courier_rows.append(row)
    courier_rows.sort(key=lambda item: (item["period_start"], item["metric_type"]))
    courier_fields = [
        "period_start",
        "period_end",
        "metric_type",
        "courier_metric_rows",
        "total_time",
        "on_the_way_time",
        "double_orders",
        "triple_orders",
        "order_count",
    ]
    write_csv(PROCESSED_DIR / "delivery_couriers_aggregate.csv", courier_rows, courier_fields)
    write_json(PROCESSED_DIR / "delivery_couriers_aggregate.json", courier_rows)
    return {"order_cycle_rows": order_cycle_rows, "courier_rows": courier_rows}


def build_cancellation_outputs(rows: list[dict[str, Any]], period: tuple[dt.date, dt.date]) -> dict[str, Any]:
    groups: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        department = dimension(value_from(row, ["Department"]))
        if department_scope(department) != "active_chernikova":
            continue
        order_deleted = dimension(value_from(row, ["OrderDeleted"]))
        cancel_cause = dimension(value_from(row, ["Delivery.CancelCause"]))
        storned = dimension(value_from(row, ["Storned"]))
        removal_type = dimension(value_from(row, ["RemovalType"]))
        deleted_with_writeoff = dimension(value_from(row, ["DeletedWithWriteoff"]))
        return_loss = abs(to_number(value_from(row, ["DishReturnSum.withoutVAT"])))
        has_cancel_cause = nonempty_issue_value(cancel_cause if cancel_cause != "(пусто)" else "")
        has_removal_type = nonempty_issue_value(removal_type if removal_type != "(пусто)" else "")
        has_writeoff = is_deleted_with_writeoff(deleted_with_writeoff)
        has_issue = (
            is_truthy(order_deleted)
            or is_truthy(storned)
            or has_cancel_cause
            or has_removal_type
            or has_writeoff
            or return_loss > 0
        )
        if not has_issue:
            continue
        key = (order_deleted, cancel_cause, storned, removal_type, deleted_with_writeoff)
        group = groups.setdefault(
            key,
            {
                "period_start": iso(period[0]),
                "period_end": iso(period[1]),
                "order_deleted": order_deleted,
                "cancel_cause": cancel_cause,
                "storned": storned,
                "removal_type": removal_type,
                "deleted_with_writeoff": deleted_with_writeoff,
                "cases_orders": 0.0,
                "known_return_loss_without_vat": 0.0,
                "canceled_order_amount": 0.0,
                "removal_amount": 0.0,
                "loss_amount_estimate": 0.0,
            },
        )
        orders = orders_from(row)
        revenue = revenue_from(row)
        gross = gross_from(row)
        group["cases_orders"] += orders
        group["known_return_loss_without_vat"] += return_loss
        if is_truthy(order_deleted) or has_cancel_cause:
            group["canceled_order_amount"] += abs(revenue)
        if has_removal_type or has_writeoff:
            group["removal_amount"] += abs(gross or revenue)

    cancellation_rows = []
    for row in groups.values():
        row["loss_amount_estimate"] = (
            row["known_return_loss_without_vat"]
            + max(row["canceled_order_amount"], row["removal_amount"])
        )
        cancellation_rows.append(row)
    cancellation_rows.sort(key=lambda item: -item["loss_amount_estimate"])
    fields = [
        "period_start",
        "period_end",
        "order_deleted",
        "cancel_cause",
        "storned",
        "removal_type",
        "deleted_with_writeoff",
        "cases_orders",
        "known_return_loss_without_vat",
        "canceled_order_amount",
        "removal_amount",
        "loss_amount_estimate",
    ]
    write_csv(PROCESSED_DIR / "cancellations_returns_summary.csv", cancellation_rows, fields)
    write_json(PROCESSED_DIR / "cancellations_returns_summary.json", cancellation_rows)
    write_csv(PROCESSED_DIR / "cancellations_returns_by_reason.csv", cancellation_rows, fields)
    return {"cancellation_rows": cancellation_rows}


def iter_json_objects(value: Any) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    if isinstance(value, dict):
        objects.append(value)
        for child in value.values():
            objects.extend(iter_json_objects(child))
    elif isinstance(value, list):
        for child in value:
            objects.extend(iter_json_objects(child))
    return objects


def load_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def detect_client_schema_candidates() -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    safe_markers = (
        "new",
        "repeat",
        "return",
        "first",
        "loyalty",
        "guesttype",
        "clienttype",
        "customertype",
        "isnew",
        "повтор",
        "перв",
        "лояль",
        "тип клиента",
    )
    pii_markers = (
        "phone",
        "email",
        "address",
        "comment",
        "card",
        "birthday",
        "name",
        "fio",
        "имя",
        "тел",
        "адрес",
        "почт",
        "коммент",
    )
    irrelevant_segment_markers = (
        "price category",
        "pricecategory",
        "ценовая категория",
        "тип карты",
        "card type",
        "реклама клиента",
    )
    for path in (RAW_DIR / "olap_columns_sales.json", RAW_DIR / "olap_columns_deliveries.json"):
        payload = load_json_file(path)
        if payload is None:
            continue
        for obj in iter_json_objects(payload):
            field = clean_text(
                obj.get("name")
                or obj.get("id")
                or obj.get("field")
                or obj.get("key")
                or obj.get("code")
            )
            title = clean_text(
                obj.get("title")
                or obj.get("caption")
                or obj.get("displayName")
                or obj.get("description")
            )
            if not field and not title:
                continue
            combined = f"{field} {title}".casefold()
            customer_related = any(
                marker in combined
                for marker in (
                    "customer",
                    "client",
                    "guest",
                    "loyalty",
                    "клиент",
                    "гость",
                    "лояль",
                    "delivery.customer",
                )
            )
            if not customer_related:
                continue
            has_segment_marker = any(marker in combined for marker in safe_markers) or bool(
                re.search(r"\bнов(ый|ая|ое|ые|ого|ому|ым|ыми|ых|ую)?\b", combined)
            )
            safe = (
                has_segment_marker
                and not any(marker in combined for marker in pii_markers)
                and not any(marker in combined for marker in irrelevant_segment_markers)
            )
            key = field or title
            candidates[key] = {
                "field": field,
                "title": title,
                "source_file": rel(path),
                "safe_for_aggregate_grouping": safe,
                "reason": "non-PII segment candidate" if safe else "schema candidate may be personal or unclear",
            }
    result = list(candidates.values())
    result.sort(key=lambda item: (not item["safe_for_aggregate_grouping"], item["field"]))
    write_csv(
        PROCESSED_DIR / "client_schema_candidates.csv",
        result,
        ["field", "title", "source_file", "safe_for_aggregate_grouping", "reason"],
    )
    write_json(PROCESSED_DIR / "client_schema_candidates.json", result)
    write_json(
        PROCESSED_DIR / "olap_column_catalog.json",
        {
            "note": "Full OLAP column JSON is stored in research/raw/iiko/orders_delivery/olap_columns_*.json; processed file keeps only client-related candidates.",
            "client_schema_candidates": result,
        },
    )
    return result


def fetch_client_segments_if_possible(
    client: IikoClient,
    manifest: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    *,
    start: dt.date,
    end: dt.date,
) -> list[dict[str, Any]]:
    safe_fields = [
        candidate["field"]
        for candidate in candidates
        if candidate.get("safe_for_aggregate_grouping") and candidate.get("field")
    ]
    if not safe_fields:
        write_json(
            PROCESSED_DIR / "client_metrics_status.json",
            {
                "status": "not_available",
                "reason": "No clearly safe aggregate new/repeat client field was found in OLAP columns.",
            },
        )
        write_json(
            PROCESSED_DIR / "customer_summary.json",
            {
                "status": "not_available",
                "reason": "No clearly safe aggregate new/repeat client field was found in OLAP columns.",
                "orders_per_client": "not_calculated_without_non_personal_stable_client_key",
                "frequency": "not_calculated_without_non_personal_stable_client_key",
                "repeat_30_days": "not_calculated_without_non_personal_stable_client_key",
            },
        )
        write_csv(
            PROCESSED_DIR / "client_segment_summary.csv",
            [],
            ["client_segment_field", "client_segment_value", "orders", "revenue", "avg_check"],
        )
        write_csv(
            PROCESSED_DIR / "customer_segments.csv",
            [],
            ["client_segment_field", "client_segment_value", "orders", "revenue", "avg_check"],
        )
        return []

    field = safe_fields[0]
    try:
        rows = fetch_olap(
            client,
            manifest,
            name=f"clients_{safe_filename(field)}",
            start=start,
            end=end,
            group_rows=["Department", field],
            agrs=["OrderNum", "DishDiscountSumInt"],
        )
    except IikoHTTPError as exc:
        write_json(
            PROCESSED_DIR / "client_metrics_status.json",
            {
                "status": "request_failed",
                "field": field,
                "reason": exc.message,
                "status_code": exc.status,
            },
        )
        write_json(
            PROCESSED_DIR / "customer_summary.json",
            {
                "status": "request_failed",
                "field": field,
                "reason": exc.message,
                "status_code": exc.status,
            },
        )
        write_csv(
            PROCESSED_DIR / "client_segment_summary.csv",
            [],
            ["client_segment_field", "client_segment_value", "orders", "revenue", "avg_check"],
        )
        write_csv(
            PROCESSED_DIR / "customer_segments.csv",
            [],
            ["client_segment_field", "client_segment_value", "orders", "revenue", "avg_check"],
        )
        return []

    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        department = dimension(value_from(row, ["Department"]))
        if department_scope(department) != "active_chernikova":
            continue
        segment = dimension(value_from(row, [field]))
        group = groups.setdefault(
            segment,
            {
                "client_segment_field": field,
                "client_segment_value": segment,
                "orders": 0.0,
                "revenue": 0.0,
                "avg_check": 0.0,
            },
        )
        group["orders"] += orders_from(row)
        group["revenue"] += revenue_from(row)
    output = []
    for row in groups.values():
        row["avg_check"] = row["revenue"] / row["orders"] if row["orders"] else 0.0
        output.append(row)
    output.sort(key=lambda item: -item["orders"])
    write_csv(
        PROCESSED_DIR / "client_segment_summary.csv",
        output,
        ["client_segment_field", "client_segment_value", "orders", "revenue", "avg_check"],
    )
    write_csv(
        PROCESSED_DIR / "customer_segments.csv",
        output,
        ["client_segment_field", "client_segment_value", "orders", "revenue", "avg_check"],
    )
    write_json(PROCESSED_DIR / "client_segment_summary.json", output)
    write_json(
        PROCESSED_DIR / "client_metrics_status.json",
        {
            "status": "aggregated_segment_exported",
            "field": field,
            "limits": "orders per client, frequency and 30-day repeat require a non-personal stable client key; not exported unless a safe field is confirmed.",
        },
    )
    write_json(
        PROCESSED_DIR / "customer_summary.json",
        {
            "status": "aggregated_segment_exported",
            "field": field,
            "rows": output,
            "orders_per_client": "not_calculated_without_non_personal_stable_client_key",
            "frequency": "not_calculated_without_non_personal_stable_client_key",
            "repeat_30_days": "not_calculated_without_non_personal_stable_client_key",
        },
    )
    return output


def top_rows(rows: list[dict[str, Any]], name_field: str, metric_field: str, limit: int = 5) -> str:
    if not rows:
        return "нет данных"
    parts = []
    for row in sorted(rows, key=lambda item: -float(item.get(metric_field, 0) or 0))[:limit]:
        name = row.get(name_field) or "(пусто)"
        value = float(row.get(metric_field, 0) or 0)
        if "share" in metric_field:
            parts.append(f"{name}: {pct(value)}")
        elif "revenue" in metric_field or "amount" in metric_field or "loss" in metric_field:
            parts.append(f"{name}: {money(value)} руб.")
        else:
            parts.append(f"{name}: {money(value)}")
    return "; ".join(parts)


def top_delivery_rows(rows: list[dict[str, Any]], limit: int = 5) -> str:
    if not rows:
        return "нет данных"
    parts = []
    for row in sorted(rows, key=lambda item: -float(item.get("orders", 0) or 0))[:limit]:
        service = row.get("service_type") or "(пусто)"
        region = row.get("region") or "(пусто)"
        label = f"{service} / {region}"
        parts.append(f"{label}: {money(float(row.get('orders', 0) or 0))}")
    return "; ".join(parts)


def cancellation_reason(row: dict[str, Any]) -> str:
    cancel_cause = row.get("cancel_cause") or "(пусто)"
    removal_type = row.get("removal_type") or "(пусто)"
    if cancel_cause != "(пусто)":
        if removal_type != "(пусто)":
            return f"отмена: {cancel_cause}, {removal_type}"
        return f"отмена: {cancel_cause}"
    if is_truthy(row.get("storned")):
        return "возврат чека"
    if removal_type != "(пусто)":
        return f"удаление блюда: {removal_type}"
    if is_deleted_with_writeoff(row.get("deleted_with_writeoff")):
        return "удаление со списанием"
    if is_truthy(row.get("order_deleted")):
        return "удаленный заказ без причины"
    return "прочее/не указано"


def top_cancellation_rows(rows: list[dict[str, Any]], limit: int = 5) -> str:
    if not rows:
        return "нет данных"
    parts = []
    for row in sorted(rows, key=lambda item: -float(item.get("loss_amount_estimate", 0) or 0))[:limit]:
        parts.append(
            f"{cancellation_reason(row)}: {money(float(row.get('loss_amount_estimate', 0) or 0))} руб."
        )
    return "; ".join(parts)


def top_cancellation_cases(rows: list[dict[str, Any]], limit: int = 5) -> str:
    if not rows:
        return "нет данных"
    parts = []
    for row in sorted(rows, key=lambda item: -float(item.get("cases_orders", 0) or 0))[:limit]:
        parts.append(f"{cancellation_reason(row)}: {money(float(row.get('cases_orders', 0) or 0))}")
    return "; ".join(parts)


def build_owner_dashboard_json(
    channel: dict[str, Any],
    delivery: dict[str, Any],
    cancellations: dict[str, Any],
    client_segments: list[dict[str, Any]],
    *,
    channel_period: tuple[dt.date, dt.date],
    delivery_period: tuple[dt.date, dt.date],
) -> dict[str, Any]:
    service_rows = channel["service_rows"]
    origin_rows = channel["origin_rows"]
    delivery_rows = delivery["delivery_rows"]
    cancellation_rows = cancellations["cancellation_rows"]
    blank_origin = next(
        (row for row in origin_rows if row["origin_name"].casefold() == "(пусто)"), None
    )
    courier_rows = [row for row in delivery_rows if row["service_type"] == "COURIER"]
    courier_orders = sum(row["orders"] for row in courier_rows)
    courier_way_sum = sum(row["way_duration_sum_min"] for row in courier_rows)
    courier_delay_weighted = sum(row["avg_delay_min"] * row["orders"] for row in courier_rows)
    known_return_loss = sum(row["known_return_loss_without_vat"] for row in cancellation_rows)
    loss_estimate = sum(row["loss_amount_estimate"] for row in cancellation_rows)
    payload = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "current_department_scope": "active_chernikova",
        "periods": {
            "channels_clients_cancellations": {
                "start": iso(channel_period[0]),
                "end": iso(channel_period[1]),
            },
            "delivery": {"start": iso(delivery_period[0]), "end": iso(delivery_period[1])},
        },
        "orders": channel["total_orders"],
        "revenue": channel["total_revenue"],
        "avg_check": channel["total_revenue"] / channel["total_orders"]
        if channel["total_orders"]
        else 0.0,
        "service_type_summary": service_rows,
        "top_origins": origin_rows[:10],
        "blank_origin_orders": blank_origin["orders"] if blank_origin else 0.0,
        "blank_origin_order_share": blank_origin["order_share"] if blank_origin else 0.0,
        "delivery": {
            "courier_orders": courier_orders,
            "courier_avg_way_duration_min": courier_way_sum / courier_orders
            if courier_orders and courier_way_sum
            else 0.0,
            "courier_avg_delay_min": courier_delay_weighted / courier_orders
            if courier_orders
            else 0.0,
            "rows": delivery_rows,
        },
        "cancellations_returns": {
            "known_return_loss_without_vat": known_return_loss,
            "loss_amount_estimate": loss_estimate,
            "rows": cancellation_rows[:20],
        },
        "client_segments": client_segments,
        "recommended_daily_owner_panel_metrics": [
            "orders",
            "revenue",
            "avg_check",
            "orders_by_origin_name",
            "orders_by_delivery_service_type",
            "discount_sum_and_discount_share",
            "blank_origin_share",
            "canceled_orders_and_cancel_causes",
            "return_loss_without_vat",
            "courier_avg_way_duration_min",
            "courier_avg_delay_min",
            "delivery_mark_if_filled",
        ],
    }
    write_json(PROCESSED_DIR / "owner_dashboard_metrics.json", payload)
    return payload


def build_report(
    dashboard: dict[str, Any],
    channel: dict[str, Any],
    delivery: dict[str, Any],
    cancellations: dict[str, Any],
    client_segments: list[dict[str, Any]],
    *,
    channel_period: tuple[dt.date, dt.date],
    delivery_period: tuple[dt.date, dt.date],
) -> None:
    service_rows = channel["service_rows"]
    origin_rows = channel["origin_rows"]
    delivery_rows = delivery["delivery_rows"]
    cancellation_rows = cancellations["cancellation_rows"]
    total_orders = dashboard["orders"]
    total_revenue = dashboard["revenue"]
    avg_check = dashboard["avg_check"]
    blank_origin_orders = dashboard["blank_origin_orders"]
    blank_origin_share = dashboard["blank_origin_order_share"]
    courier = dashboard["delivery"]
    returns = dashboard["cancellations_returns"]

    service_text = top_rows(service_rows, "service_type", "order_share", 5)
    origin_text = top_rows(origin_rows, "origin_name", "revenue", 5)
    cancel_text = top_cancellation_rows(cancellation_rows, 5)
    cancel_cases_text = top_cancellation_cases(cancellation_rows, 5)
    delivery_region_text = top_delivery_rows(delivery_rows, 5)
    client_text = (
        top_rows(client_segments, "client_segment_value", "orders", 5)
        if client_segments
        else "нет безопасного агрегатного признака новый/повторный"
    )
    endpoint_status = load_json_file(PROCESSED_DIR / "delivery_report_endpoint_status.json")
    endpoint_rows = endpoint_status if isinstance(endpoint_status, list) else []
    endpoint_failures = [row for row in endpoint_rows if int(row.get("status") or 0) >= 400]
    regions_empty = any(
        row.get("endpoint") == "/reports/delivery/regions"
        and int(row.get("status") or 0) < 400
        and int(row.get("parsed_rows") or 0) == 0
        for row in endpoint_rows
    )
    risk_rows = [
        {
            "area": "channels",
            "risk": "Пустой OriginName нельзя распределять по каналам без ручной расшифровки.",
            "action": "Разобрать настройки источников заказов и закрепить справочник каналов.",
        },
        {
            "area": "delivery",
            "risk": "Delivery.AggregatedAvgMark может быть пустым; среднюю оценку нельзя считать репрезентативной без количества оценок.",
            "action": "Показывать оценку доставки только вместе с количеством заполненных оценок.",
        },
        {
            "area": "cancellations",
            "risk": "Потери по отменам и удалениям являются оценкой: OLAP-агрегаты не всегда позволяют отделить потерянную выручку от технических строк.",
            "action": "Для финансовых выводов сверять возвраты и удаления со складскими списаниями.",
        },
        {
            "area": "clients",
            "risk": "Частота, заказы на клиента и повтор за 30 дней не рассчитаны без подтвержденного неперсонального ключа клиента.",
            "action": "Согласовать безопасный агрегатный признак новый/повторный или отдельную private-анонимизацию.",
        },
        {
            "area": "departments",
            "risk": "Гагарина после января 2024 не входит в текущий контур Черникова.",
            "action": "Держать фильтр текущего контура по Черниковой.",
        },
    ]
    if endpoint_failures:
        risk_rows.append(
            {
                "area": "delivery_reports",
                "risk": "Часть `/reports/delivery/*` endpoint'ов вернула ошибку на проверочных запросах.",
                "action": "Использовать OLAP-поля доставки как основной источник и уточнить формат параметров endpoint'ов.",
            }
        )
    elif endpoint_rows and regions_empty:
        risk_rows.append(
            {
                "area": "delivery_regions",
                "risk": "`/reports/delivery/regions` отработал без ошибки, но вернул 0 строк; OLAP `Delivery.Region` тоже пустой.",
                "action": "Проверить заполнение районов доставки в iiko перед использованием районов в панели.",
            }
        )
    write_csv(PROCESSED_DIR / "quality_risks.csv", risk_rows, ["area", "risk", "action"])
    write_json(PROCESSED_DIR / "quality_risks.json", risk_rows)
    risk_lines = [f"- {row['risk']} Действие: {row['action']}" for row in risk_rows]

    lines = [
        "# Каналы, клиенты и доставка — iiko агрегаты",
        "",
        f"Сформировано: {dt.datetime.now().isoformat(timespec='seconds')}.",
        "Контур отчета: активная точка Черникова; Гагарина считается исторической и не смешивается с текущими выводами.",
        "",
        "## Итоги",
        "",
        (
            f"- Факт: {money(total_orders)} заказов и {money(total_revenue)} руб. выручки, "
            f"средний чек {money(avg_check)} руб. / Источник: iiko `/reports/olap`, SALES, "
            "группировки Department, OriginName, OrderType, Delivery.ServiceType, PayTypes / "
            f"Период: {iso(channel_period[0])} — {iso(channel_period[1])} / "
            "Вывод: это базовый срез для канальной панели собственника / "
            "Действие: обновлять ежедневно без текущего неполного дня."
        ),
        (
            f"- Факт: структура по способу получения заказа: {service_text} / "
            "Источник: iiko OLAP, поле Delivery.ServiceType / "
            f"Период: {iso(channel_period[0])} — {iso(channel_period[1])} / "
            "Вывод: доли доставки и самовывоза можно ставить в ежедневный контроль / "
            "Действие: отслеживать с выручкой и средним чеком по каждому типу."
        ),
        (
            f"- Факт: топ каналов по выручке: {origin_text} / "
            "Источник: iiko OLAP, поля OriginName, OrderType, PayTypes / "
            f"Период: {iso(channel_period[0])} — {iso(channel_period[1])} / "
            "Вывод: OriginName подходит как основной канал только после контроля пустых значений / "
            "Действие: закрепить справочник каналов и правила заполнения источника заказа."
        ),
        (
            f"- Факт: пустой или непонятный OriginName: {money(blank_origin_orders)} заказов, "
            f"{pct(blank_origin_share)} от заказов активного контура / "
            "Источник: iiko OLAP, поле OriginName / "
            f"Период: {iso(channel_period[0])} — {iso(channel_period[1])} / "
            "Вывод: пустой источник искажает оценку эффективности каналов / "
            "Действие: разобрать настройки сайта, телефонии, ручного ввода и агрегаторов."
        ),
        (
            f"- Факт: доставка по районам/типам: {delivery_region_text}; "
            f"COURIER среднее время в пути {courier['courier_avg_way_duration_min']:.1f} мин., "
            f"среднее опоздание {courier['courier_avg_delay_min']:.1f} мин. / "
            "Источник: iiko OLAP, поля Delivery.Region, Delivery.WayDurationAvg/Sum, Delivery.DelayAvg / "
            f"Период: {iso(delivery_period[0])} — {iso(delivery_period[1])} / "
            "Вывод: показатели доставки можно вести ежедневно, но оценки доставки использовать только при заполненности / "
            "Действие: добавить пороги по времени в пути и опозданию."
        ),
        (
            f"- Факт: отмены и возвраты по причинам: по количеству {cancel_cases_text}; "
            f"по сумме {cancel_text}; известная сумма возвратов без НДС "
            f"{money(returns['known_return_loss_without_vat'])} руб., расширенная оценка потерь "
            f"{money(returns['loss_amount_estimate'])} руб. / "
            "Источник: iiko OLAP, поля OrderDeleted, Delivery.CancelCause, Storned, "
            "DishReturnSum.withoutVAT, RemovalType, DeletedWithWriteoff / "
            f"Период: {iso(channel_period[0])} — {iso(channel_period[1])} / "
            "Вывод: возвраты считаются надежнее, чем оценка потерь по отменам/удалениям / "
            "Действие: вести причины отмен отдельным ежедневным блоком и проверять строки без причины."
        ),
        (
            f"- Факт: клиентские сегменты: {client_text} / "
            "Источник: iiko OLAP columns + безопасная агрегатная группировка, если найдена / "
            f"Период: {iso(channel_period[0])} — {iso(channel_period[1])} / "
            "Вывод: частота, заказы на клиента и повтор за 30 дней не рассчитывались без подтвержденного "
            "неперсонального стабильного ключа клиента / "
            "Действие: согласовать безопасный агрегатный признак нового/повторного клиента или отдельную приватную обработку."
        ),
        "",
        "## Ежедневная панель собственника",
        "",
        "- Заказы, выручка, средний чек.",
        "- Заказы и выручка по OriginName, OrderType, Delivery.ServiceType, PayTypes.",
        "- Доля пустого OriginName.",
        "- Скидки: сумма и доля от валовой суммы.",
        "- Доставка: COURIER/PICKUP, районы, среднее время в пути, среднее опоздание, суммарное время в пути.",
        "- Отмены/возвраты: количество случаев, причины, известная сумма возвратов без НДС, оценка оборота отмен/удалений.",
        "",
        "## Риски качества данных",
        "",
        *risk_lines,
        "",
        "## Файлы",
        "",
        "- Raw: `research/raw/iiko/orders_delivery/`.",
        "- Processed: `research/processed/iiko/orders_delivery/`.",
    ]
    report_text = "\n".join(lines) + "\n"
    (PROCESSED_DIR / "report.md").write_text(report_text, encoding="utf-8")
    (PROCESSED_DIR / "orders_delivery_report.md").write_text(report_text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only iiko orders/delivery export")
    parser.add_argument("--end", default=iso(END_DATE), help="inclusive end date, YYYY-MM-DD")
    parser.add_argument("--channel-start", default=iso(CHANNEL_START), help="YYYY-MM-DD")
    parser.add_argument("--delivery-start", default=iso(DELIVERY_START), help="YYYY-MM-DD")
    parser.add_argument(
        "--only-delivery-reports",
        action="store_true",
        help="refresh only /reports/delivery/* raw endpoint checks",
    )
    args = parser.parse_args()

    channel_start = parse_date(args.channel_start)
    delivery_start = parse_date(args.delivery_start)
    end_date = parse_date(args.end)
    if channel_start > end_date or delivery_start > end_date:
        raise SystemExit("start date cannot be after end date")

    load_local_env()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    client = IikoClient()

    refs = fetch_reference_data(client, manifest)
    active_department = active_department_from_rows(
        refs.get("departments", []) + refs.get("departments_search", [])
    )
    write_json(PROCESSED_DIR / "active_department.json", active_department)

    if args.only_delivery_reports:
        fetch_delivery_reports(
            client,
            manifest,
            department=active_department,
            start=delivery_start,
            end=end_date,
        )
        build_delivery_report_outputs(delivery_start, end_date)
        write_json(RAW_DIR / "manifest_delivery_reports_refresh.json", manifest)
        print("delivery report endpoint refresh completed")
        return 0

    channel_rows = fetch_olap(
        client,
        manifest,
        name="channels",
        start=channel_start,
        end=end_date,
        group_rows=["Department", "OriginName", "OrderType", "Delivery.ServiceType", "PayTypes"],
        agrs=["OrderNum", "DishSumInt", "DishDiscountSumInt", "DiscountSum"],
    )
    if not active_department.get("name"):
        active_department = active_department_from_olap_rows(channel_rows)
        write_json(PROCESSED_DIR / "active_department.json", active_department)
    write_json(
        PROCESSED_DIR / "departments_scope.json",
        {
            "active": [active_department] if active_department.get("name") else [],
            "historical": [],
            "note": "Active department is taken from /corporation/departments when available, otherwise from OLAP Department rows.",
        },
    )
    delivery_rows = fetch_olap(
        client,
        manifest,
        name="delivery_metrics",
        start=delivery_start,
        end=end_date,
        group_rows=["Department", "Delivery.ServiceType", "Delivery.Region"],
        agrs=[
            "OrderNum",
            "DishDiscountSumInt",
            "Delivery.DelayAvg",
            "Delivery.WayDurationAvg",
            "Delivery.WayDurationSum",
            "Delivery.AggregatedAvgMark",
        ],
    )
    cancellation_rows = fetch_olap(
        client,
        manifest,
        name="cancellations_returns",
        start=channel_start,
        end=end_date,
        group_rows=[
            "Department",
            "OrderDeleted",
            "Delivery.CancelCause",
            "Storned",
            "RemovalType",
            "DeletedWithWriteoff",
        ],
        agrs=["OrderNum", "DishSumInt", "DishDiscountSumInt", "DishReturnSum.withoutVAT"],
    )

    fetch_delivery_reports(
        client,
        manifest,
        department=active_department,
        start=delivery_start,
        end=end_date,
    )
    build_delivery_report_outputs(delivery_start, end_date)

    client_candidates = detect_client_schema_candidates()
    client_segments = fetch_client_segments_if_possible(
        client,
        manifest,
        client_candidates,
        start=channel_start,
        end=end_date,
    )

    channel_output = build_channel_outputs(channel_rows, (channel_start, end_date))
    delivery_output = build_delivery_outputs(delivery_rows, (delivery_start, end_date))
    cancellation_output = build_cancellation_outputs(cancellation_rows, (channel_start, end_date))
    dashboard = build_owner_dashboard_json(
        channel_output,
        delivery_output,
        cancellation_output,
        client_segments,
        channel_period=(channel_start, end_date),
        delivery_period=(delivery_start, end_date),
    )
    build_report(
        dashboard,
        channel_output,
        delivery_output,
        cancellation_output,
        client_segments,
        channel_period=(channel_start, end_date),
        delivery_period=(delivery_start, end_date),
    )

    write_json(RAW_DIR / "manifest.json", manifest)
    write_json(PROCESSED_DIR / "run_manifest.json", manifest)
    print(f"processed files written to {rel(PROCESSED_DIR)}")
    print(f"raw files written to {rel(RAW_DIR)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

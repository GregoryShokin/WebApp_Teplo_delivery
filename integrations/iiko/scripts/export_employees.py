#!/usr/bin/env python3
"""Read-only discovery/export for iiko employee attendance and payroll data.

The script performs only GET requests against iikoServer. The shared
IikoClient can call POST /auth when a configured token is missing or expired.
Secrets are loaded from .env/ENV and are never printed or written to outputs.

Raw responses stay under research/raw/iiko/employees_research/ (gitignored).
Processed files contain only field structure, endpoint statuses and
anonymized aggregates keyed by employee_id_hash.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import datetime as dt
import hashlib
import html
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from export_orders_delivery import IikoClient, IikoHTTPError, PROJECT_ROOT, load_local_env


RAW_DIR = PROJECT_ROOT / "research/raw/iiko/employees_research"
PROCESSED_DIR = PROJECT_ROOT / "research/processed/iiko/employees"

SEARCH_KEYWORDS = [
    "employee",
    "schedule",
    "attendance",
    "shift",
    "clockin",
    "clockout",
    "labour",
    "labor",
    "worktime",
    "timesheet",
    "salary",
    "payroll",
]

ATTENDANCE_ENDPOINT = "/employees/attendance"
SCHEDULE_ENDPOINT = "/employees/schedule"

ACTIVE_DEPARTMENT_MARKER = "черников"
HISTORICAL_DEPARTMENT_MARKER = "гагарин"
HASH_SALT = "teplo-iiko-employees"


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\xa0", " ").strip()
    if text.casefold() in {"none", "null", "nan"}:
        return ""
    return text


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def iso(value: dt.date) -> str:
    return value.isoformat()


def month_chunks(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    chunks: list[tuple[dt.date, dt.date]] = []
    current = start
    while current <= end:
        last_day = calendar.monthrange(current.year, current.month)[1]
        chunk_end = min(end, dt.date(current.year, current.month, last_day))
        chunks.append((current, chunk_end))
        current = chunk_end + dt.timedelta(days=1)
    return chunks


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


def sanitize_message(value: str) -> str:
    text = clean_text(value)
    text = re.sub(r"([?&](?:key|pass|password|login)=)[^&\s\"']+", r"\1<redacted>", text)
    text = re.sub(r"\b[0-9a-f]{40}\b", "<sha1-redacted>", text, flags=re.I)
    return text[:1000]


def safe_filename(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value)


def response_extension(data: bytes, fallback: str = "txt") -> str:
    stripped = data.lstrip()
    if stripped.startswith((b"{", b"[")):
        return "json"
    if stripped.startswith(b"<"):
        return "xml"
    return fallback


def redact_id_path(value: str) -> str:
    text = re.sub(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        "{id}",
        value,
        flags=re.I,
    )
    return text


def parse_json_records(data: bytes) -> list[dict[str, Any]]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []

    def records(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            if all(isinstance(item, dict) for item in value):
                return list(value)
            result: list[dict[str, Any]] = []
            for item in value:
                result.extend(records(item))
            return result
        if isinstance(value, dict):
            for key in ("data", "rows", "items", "records", "result", "response"):
                if key in value:
                    found = records(value[key])
                    if found:
                        return found
            return [value]
        return []

    return records(payload)


def xml_item_blocks(text: str, item_tag: str) -> list[str]:
    tag = re.escape(item_tag)
    pattern = re.compile(
        rf"<(?:[A-Za-z_][\w.-]*:)?{tag}\b[^>]*>(.*?)</(?:[A-Za-z_][\w.-]*:)?{tag}>",
        re.DOTALL,
    )
    return pattern.findall(text)


def parse_xml_items(data: bytes, item_tag: str) -> list[dict[str, str]]:
    text = data.decode("utf-8", "replace")
    rows: list[dict[str, str]] = []
    child_pattern = re.compile(
        r"<(?P<tag>[A-Za-z_][\w.:-]*)\b[^>]*>(?P<value>.*?)</(?P=tag)>"
        r"|<(?P<empty>[A-Za-z_][\w.:-]*)\b[^>]*/>",
        re.DOTALL,
    )
    for block in xml_item_blocks(text, item_tag):
        row: dict[str, str] = {}
        for match in child_pattern.finditer(block):
            raw_tag = match.group("tag") or match.group("empty")
            if not raw_tag:
                continue
            key = raw_tag.rsplit(":", 1)[-1]
            value = "" if match.group("empty") else (match.group("value") or "")
            # Keep this parser shallow. Nested employee/contact blocks may contain
            # PII and are not needed for processed outputs.
            if "<" in value:
                continue
            row[key] = clean_text(value)
        if row:
            rows.append(row)
    return rows


def records_from_response(endpoint: str, data: bytes) -> list[dict[str, Any]]:
    stripped = data.lstrip()
    if stripped.startswith((b"{", b"[")):
        return parse_json_records(data)

    endpoint_clean = endpoint.split("?", 1)[0]
    if endpoint_clean.endswith("/attendance/types"):
        return parse_xml_items(data, "attendanceType")
    if "/employees/attendance" in endpoint_clean:
        return parse_xml_items(data, "attendance")
    if endpoint_clean.endswith("/schedule/types") or endpoint_clean.endswith("/entities/scheduleTypes"):
        return parse_xml_items(data, "scheduleType")
    if "/employees/schedule" in endpoint_clean:
        return parse_xml_items(data, "schedule")
    if "/employees/salary" in endpoint_clean:
        return parse_xml_items(data, "salary")
    if "/employees/roles" in endpoint_clean:
        return parse_xml_items(data, "role")
    if endpoint_clean in {"/employees", "/employees/search"} or "/employees/byDepartment" in endpoint_clean:
        employees = parse_xml_items(data, "employee")
        return employees or parse_xml_items(data, "employeeDto")
    if endpoint_clean.startswith("/corporation/departments"):
        return parse_xml_items(data, "corporateItemDto")
    return []


def fetch_get(
    client: IikoClient,
    manifest: list[dict[str, Any]],
    *,
    endpoint: str,
    raw_name: str,
    params: dict[str, Any] | None = None,
    period: tuple[dt.date, dt.date] | None = None,
    date_format: str = "",
    mandatory_params: str = "",
    note: str = "",
    extension_override: str | None = None,
) -> tuple[int | None, bytes, list[dict[str, Any]], Path]:
    params = params or {}
    display_endpoint = redact_id_path(endpoint)
    try:
        status, data = client.request(endpoint, params=params)
        extension = extension_override or response_extension(data)
        path = RAW_DIR / f"{raw_name}.{extension}"
        write_bytes(path, data)
        records = records_from_response(endpoint, data)
        manifest.append(
            {
                "endpoint": display_endpoint,
                "method": "GET",
                "status_code": status,
                "returned_records": len(records),
                "date_format": date_format,
                "mandatory_params": mandatory_params,
                "notes": note,
                "raw_file": rel(path),
            }
        )
        time.sleep(0.2)
        print(f"GET {display_endpoint}: {status}, records={len(records)}")
        return status, data, records, path
    except (IikoHTTPError, TimeoutError, OSError) as raw_exc:
        status = raw_exc.status if isinstance(raw_exc, IikoHTTPError) else None
        message = raw_exc.message if isinstance(raw_exc, IikoHTTPError) else str(raw_exc)
        path = RAW_DIR / f"{raw_name}_error.json"
        payload = {
            "endpoint": display_endpoint,
            "status_code": status,
            "message": sanitize_message(message),
            "note": note,
        }
        if period:
            payload["period_start"] = iso(period[0])
            payload["period_end"] = iso(period[1])
        write_json(path, payload)
        manifest.append(
            {
                "endpoint": display_endpoint,
                "method": "GET",
                "status_code": status if status is not None else "",
                "returned_records": 0,
                "date_format": date_format,
                "mandatory_params": mandatory_params,
                "notes": f"error: {note}".strip(),
                "raw_file": rel(path),
            }
        )
        time.sleep(0.2)
        print(f"GET {display_endpoint}: error status={status}")
        return status, b"", [], path


def parse_wadl_methods(text: str) -> list[dict[str, str]]:
    token_pattern = re.compile(r"<(?P<close>/)?(?P<tag>resource|method)\b(?P<attrs>[^>]*?)(?P<self>/)?>")
    stack: list[str] = []
    methods: list[dict[str, str]] = []

    def attr(attrs: str, name: str) -> str:
        match = re.search(rf'{name}\s*=\s*"([^"]*)"', attrs)
        return match.group(1) if match else ""

    for match in token_pattern.finditer(text):
        tag = match.group("tag")
        attrs = match.group("attrs") or ""
        if match.group("close"):
            if tag == "resource" and stack:
                stack.pop()
            continue
        if tag == "resource":
            stack.append(attr(attrs, "path"))
            if match.group("self") and stack:
                stack.pop()
            continue
        if tag == "method":
            method = attr(attrs, "name") or attr(attrs, "id")
            path = "/" + "/".join(part.strip("/") for part in stack if part.strip("/"))
            methods.append({"method": method, "path": path})
    return methods


def write_wadl_matches(wadl_text: str) -> list[dict[str, str]]:
    keyword_re = re.compile("|".join(re.escape(word) for word in SEARCH_KEYWORDS), re.I)
    matches = [
        item
        for item in parse_wadl_methods(wadl_text)
        if keyword_re.search(item["path"]) or keyword_re.search(item["method"])
    ]
    matches = sorted({(row["method"], row["path"]) for row in matches}, key=lambda item: item[1])
    path = RAW_DIR / "wadl_employees_matches.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "WADL employee/payroll/schedule keyword matches",
        f"keywords: {', '.join(SEARCH_KEYWORDS)}",
        f"count: {len(matches)}",
        "",
    ]
    lines.extend(f"{method}\t{path_value}" for method, path_value in matches)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [{"method": method, "path": path_value} for method, path_value in matches]


def department_scope(name: str) -> str:
    lowered = clean_text(name).casefold()
    if ACTIVE_DEPARTMENT_MARKER in lowered:
        return "active_chernikova"
    if HISTORICAL_DEPARTMENT_MARKER in lowered:
        return "historical_gagarina"
    if lowered:
        return "other_department"
    return "unknown_department"


def find_active_department(departments: list[dict[str, Any]]) -> dict[str, str]:
    for row in departments:
        name = clean_text(row.get("name"))
        if department_scope(name) == "active_chernikova":
            return {"id": clean_text(row.get("id")), "name": name}
    return {"id": "", "name": ""}


def parse_iiko_datetime(value: Any) -> dt.datetime | None:
    text = clean_text(value)
    if not text:
        return None
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            return dt.datetime.fromisoformat(candidate)
        except ValueError:
            continue
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(text[:19], pattern)
        except ValueError:
            continue
    return None


def minutes_between(start_value: Any, end_value: Any) -> int | None:
    start = parse_iiko_datetime(start_value)
    end = parse_iiko_datetime(end_value)
    if not start or not end or end < start:
        return None
    return int(round((end - start).total_seconds() / 60))


def employee_hash(employee_id: str) -> str:
    return hashlib.sha256(f"{HASH_SALT}:{employee_id}".encode("utf-8")).hexdigest()[:12]


def role_code(row: dict[str, Any]) -> str:
    code = clean_text(row.get("code"))
    name = clean_text(row.get("name"))
    return code or name or "unknown_role"


def breakdown_text(counter: Counter[str]) -> str:
    return ";".join(f"{key}:{counter[key]}" for key in sorted(counter))


def build_attendance_monthly(
    attendance_by_month: dict[str, list[dict[str, Any]]],
    schedule_by_month: dict[str, list[dict[str, Any]]],
    roles: dict[str, dict[str, Any]],
    attendance_types: dict[str, dict[str, Any]],
    periods: list[tuple[dt.date, dt.date]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    schedule_minutes: defaultdict[tuple[str, str, str, str], int] = defaultdict(int)
    total_schedule_rows = 0
    for month, rows in schedule_by_month.items():
        for row in rows:
            total_schedule_rows += 1
            start = parse_iiko_datetime(row.get("dateFrom"))
            if not start:
                continue
            role = roles.get(clean_text(row.get("roleId")), {})
            employee_id = clean_text(row.get("employeeId"))
            dept = department_scope(clean_text(row.get("departmentName")))
            key = (month, employee_hash(employee_id), role_code(role), dept)
            minutes = minutes_between(row.get("dateFrom"), row.get("dateTo"))
            if minutes:
                schedule_minutes[key] += minutes

    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    skipped_out_of_period = 0
    open_rows = 0
    total_attendance_rows = 0
    active_attendance_rows = 0
    period_by_month = {start.strftime("%Y-%m"): (start, end) for start, end in periods}
    for month, rows in attendance_by_month.items():
        period = period_by_month.get(month)
        for row in rows:
            total_attendance_rows += 1
            start = parse_iiko_datetime(row.get("dateFrom"))
            if not start or not period:
                skipped_out_of_period += 1
                continue
            if not (period[0] <= start.date() <= period[1]):
                skipped_out_of_period += 1
                continue
            dept = department_scope(clean_text(row.get("departmentName")))
            if dept == "active_chernikova":
                active_attendance_rows += 1
            employee_id = clean_text(row.get("employeeId"))
            role = roles.get(clean_text(row.get("roleId")), {})
            type_row = attendance_types.get(clean_text(row.get("attendanceTypeId")), {})
            type_code = clean_text(type_row.get("code")) or clean_text(row.get("attendanceType")) or "unknown"
            key = (month, employee_hash(employee_id), role_code(role), dept)
            group = groups.setdefault(
                key,
                {
                    "month": month,
                    "employee_id_hash": employee_hash(employee_id),
                    "role_code": role_code(role),
                    "department": dept,
                    "planned_minutes": "",
                    "fact_minutes": 0,
                    "attendance_type_count_breakdown": Counter(),
                    "status_flags": set(),
                    "source_endpoint": ATTENDANCE_ENDPOINT,
                },
            )
            minutes = minutes_between(row.get("dateFrom"), row.get("dateTo"))
            if minutes is None:
                open_rows += 1
                group["status_flags"].add("open_without_dateTo")
            else:
                group["fact_minutes"] += minutes
            group["attendance_type_count_breakdown"][type_code] += 1

    output: list[dict[str, Any]] = []
    schedule_zero = total_schedule_rows == 0
    for key, row in groups.items():
        planned = schedule_minutes.get(key)
        status_flags = set(row.pop("status_flags"))
        if planned:
            row["planned_minutes"] = planned
        elif schedule_zero:
            status_flags.add("schedule_endpoint_returned_zero")
        else:
            status_flags.add("no_matching_schedule")
        if not status_flags:
            status_flags.add("ok")
        row["attendance_type_count_breakdown"] = breakdown_text(row["attendance_type_count_breakdown"])
        row["status"] = ";".join(sorted(status_flags))
        output.append(row)

    output.sort(key=lambda item: (item["month"], item["department"], item["role_code"], item["employee_id_hash"]))
    stats = {
        "total_attendance_rows_raw": total_attendance_rows,
        "active_attendance_rows": active_attendance_rows,
        "skipped_out_of_period": skipped_out_of_period,
        "open_rows": open_rows,
        "total_schedule_rows_raw": total_schedule_rows,
    }
    return output, stats


PII_FIELD_RE = re.compile(
    r"name|phone|passport|card|pin|login|user|address|email|taxpayer|settlementAccount|account",
    re.I,
)


BUSINESS_MEANING = {
    "id": "technical row identifier in iiko",
    "employeeId": "employee foreign key; stored only as employee_id_hash in processed",
    "roleId": "employee role foreign key",
    "dateFrom": "attendance or schedule interval start",
    "dateTo": "attendance or schedule interval end; missing means open/unclosed entry",
    "attendanceTypeId": "attendance type foreign key",
    "attendanceType": "short attendance type code from iiko",
    "comment": "free-form comment; may contain PII and is not exported to processed aggregates",
    "departmentId": "department foreign key",
    "departmentName": "department/location; mapped to active_chernikova/historical_gagarina",
    "personalDateFrom": "personal attendance interval start before department-level normalization",
    "personalDateTo": "personal attendance interval end before department-level normalization",
    "created": "row creation timestamp",
    "modified": "row modification timestamp",
    "userModified": "user who last modified the row; redacted in processed schema",
    "code": "business code of reference item",
    "name": "reference display name; redacted for employee records",
    "payment": "current salary/payment setting",
    "paymentPerHour": "hourly role payment setting",
    "steadySalary": "fixed salary role setting",
    "scheduleType": "role schedule type",
    "payRate": "attendance type pay multiplier",
    "status": "reference active/status flag, not payroll approval status",
    "startTime": "planned schedule type start time",
    "lengthMinutes": "planned schedule type duration",
    "tariff": "schedule type tariff",
    "overtime": "schedule type overtime flag",
    "WaiterName": "employee/waiter dimension in SALES preset; treat as PII and map only through a protected employee lookup",
    "DishDiscountSumInt": "sales revenue after discounts in iiko OLAP",
    "DishSumInt": "sales revenue before discounts in iiko OLAP",
}


def infer_type(values: list[str]) -> str:
    nonempty = [clean_text(value) for value in values if clean_text(value)]
    if not nonempty:
        return "string"
    if all(value.casefold() in {"true", "false"} for value in nonempty):
        return "boolean"
    if all(re.fullmatch(r"-?\d+(?:[.,]\d+)?", value.replace(" ", "")) for value in nonempty):
        return "decimal"
    if all(parse_iiko_datetime(value) for value in nonempty[:20]):
        return "datetime"
    if all(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value, re.I) for value in nonempty):
        return "uuid"
    return "string"


def redacted_example(field: str, values: list[str], endpoint: str) -> str:
    nonempty = [clean_text(value) for value in values if clean_text(value)]
    if not nonempty:
        return ""
    field_lower = field.casefold()
    if field_lower == "employeeid":
        return "<employee_id_hash>"
    if field_lower in {"waitername", "employeename", "cashiername"}:
        return "<redacted PII>"
    looks_like_uuid_list = all(
        re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            value,
            re.I,
        )
        for value in nonempty[:20]
    )
    if field_lower == "id" or field_lower.endswith("id") or field_lower.endswith("ids") or looks_like_uuid_list:
        return "<id>"
    employee_record_endpoint = endpoint in {"/employees", "/employees/search"} or "/employees/byDepartment" in endpoint
    if employee_record_endpoint and PII_FIELD_RE.search(field) and field not in {"departmentName"}:
        return "<redacted PII>"
    if field == "userModified":
        return "<redacted user>"
    if field == "departmentName":
        return department_scope(nonempty[0])
    if field == "comment":
        return "<redacted comment>"
    return nonempty[0][:80]


def build_field_schema(samples: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for endpoint, rows in samples.items():
        fields = sorted({str(key) for row in rows for key in row.keys()})
        for field in fields:
            values = [clean_text(row.get(field)) for row in rows]
            nullable = any(not value for value in values)
            output.append(
                {
                    "endpoint": endpoint,
                    "field_path": field,
                    "type": infer_type(values),
                    "nullable": str(nullable).lower(),
                    "example_redacted": redacted_example(field, values, endpoint),
                    "business_meaning": BUSINESS_MEANING.get(field, "field observed in iiko response; confirm business semantics before using in payroll rules"),
                }
            )
    output.sort(key=lambda row: (row["endpoint"], row["field_path"]))
    return output


def parse_presets_staff(data: bytes) -> list[dict[str, Any]]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    rows = payload if isinstance(payload, list) else parse_json_records(data)
    keywords = ("персон", "сотруд", "смен", "явк", "зарп", "salary", "employee", "labour", "labor", "attendance", "schedule", "выруч")
    filtered: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = " ".join(clean_text(row.get(key)) for key in ("id", "name", "reportType", "description")).casefold()
        if any(keyword in text for keyword in keywords):
            filtered.append({key: row.get(key) for key in ("id", "name", "reportType", "description") if key in row})
    return filtered


def first_employee_id(rows: list[dict[str, Any]]) -> str:
    for row in rows:
        if department_scope(clean_text(row.get("departmentName"))) == "active_chernikova":
            employee_id = clean_text(row.get("employeeId"))
            if employee_id:
                return employee_id
    for row in rows:
        employee_id = clean_text(row.get("employeeId"))
        if employee_id:
            return employee_id
    return ""


def write_inventory(manifest: list[dict[str, Any]]) -> None:
    rows = []
    for row in manifest:
        notes = clean_text(row.get("notes"))
        if (
            row.get("returned_records") == 0
            and not notes.startswith("error")
            and row.get("endpoint") not in {"/application.wadl"}
        ):
            notes = f"{notes}; endpoint_returned_zero".strip("; ")
        rows.append(
            {
                "endpoint": row.get("endpoint", ""),
                "method": row.get("method", "GET"),
                "status_code": row.get("status_code", ""),
                "returned_records": row.get("returned_records", 0),
                "date_format": row.get("date_format", ""),
                "mandatory_params": row.get("mandatory_params", ""),
                "notes": notes,
            }
        )
    write_csv(
        PROCESSED_DIR / "endpoints_inventory.csv",
        rows,
        ["endpoint", "method", "status_code", "returned_records", "date_format", "mandatory_params", "notes"],
    )


def write_report(
    *,
    manifest: list[dict[str, Any]],
    monthly_rows: list[dict[str, Any]],
    stats: dict[str, Any],
    presets_staff: list[dict[str, Any]],
    generated_at: str,
    end_date: dt.date,
) -> None:
    by_month_records: defaultdict[str, int] = defaultdict(int)
    by_month_minutes: defaultdict[str, int] = defaultdict(int)
    by_month_people: defaultdict[str, set[str]] = defaultdict(set)
    for row in monthly_rows:
        if row["department"] != "active_chernikova":
            continue
        by_month_records[row["month"]] += sum(
            int(part.split(":", 1)[1])
            for part in clean_text(row["attendance_type_count_breakdown"]).split(";")
            if ":" in part
        )
        by_month_minutes[row["month"]] += int(row["fact_minutes"] or 0)
        by_month_people[row["month"]].add(row["employee_id_hash"])

    lines = [
        "# iiko employees research report",
        "",
        f"Generated: {generated_at}. Period end: {end_date.isoformat()}.",
        "",
        "## Main finding",
        "",
        "`/employees/attendance` is the working iiko journal of employee attendance. "
        "The previous zero-hour result was caused by parser coverage: raw XML contains `<attendance>` items, while the old parser counted only row-like tags.",
        "",
        "## Active contour counts",
        "",
        "| Month | Attendance records | Fact hours | Employees |",
        "| --- | ---: | ---: | ---: |",
    ]
    for month in sorted(by_month_records):
        if month < "2026-02":
            continue
        hours = by_month_minutes[month] / 60
        lines.append(f"| {month} | {by_month_records[month]} | {hours:.1f} | {len(by_month_people[month])} |")

    lines.extend(
        [
            "",
            "## Data quality",
            "",
            f"- Raw attendance rows parsed: {stats.get('total_attendance_rows_raw', 0)}.",
            f"- Active Черникова rows after period filtering: {stats.get('active_attendance_rows', 0)}.",
            f"- Rows skipped because `dateFrom` was outside the requested month: {stats.get('skipped_out_of_period', 0)}.",
            f"- Open rows without `dateTo`: {stats.get('open_rows', 0)}.",
            f"- Schedule rows parsed: {stats.get('total_schedule_rows_raw', 0)}; empty schedule is marked as `schedule_endpoint_returned_zero`, not as confirmed zero plan.",
            "",
            "## Endpoint notes",
            "",
        ]
    )
    interesting = [
        row
        for row in manifest
        if row["endpoint"]
        in {
            "/employees/attendance",
            "/employees/schedule",
            "/employees/salary",
            "/v2/payrolls/list",
            "/v2/reports/olap/columns",
            "/v2/reports/olap/presets",
        }
    ]
    for row in interesting[:40]:
        lines.append(
            f"- `{row['endpoint']}`: status={row.get('status_code')}, records={row.get('returned_records')}, note={clean_text(row.get('notes')) or 'ok'}."
        )
    if presets_staff:
        lines.extend(["", "## Staff/revenue related OLAP presets", ""])
        for preset in presets_staff:
            lines.append(
                f"- `{preset.get('id')}`: {clean_text(preset.get('name'))} ({clean_text(preset.get('reportType'))})."
            )

    lines.extend(
        [
            "",
            "## Files",
            "",
            "- `attendance_monthly.csv`: anonymized employee-month-role aggregates.",
            "- `attendance_field_schema.csv`: redacted field catalogue.",
            "- `endpoints_inventory.csv`: tried endpoint inventory.",
        ]
    )
    (PROCESSED_DIR / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(end_date: dt.date) -> None:
    load_local_env()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    client = IikoClient()
    manifest: list[dict[str, Any]] = []
    samples: dict[str, list[dict[str, Any]]] = {}
    generated_at = dt.datetime.now().isoformat(timespec="seconds")

    _, wadl_data, _, _ = fetch_get(
        client,
        manifest,
        endpoint="/application.wadl",
        raw_name="application",
        note="WADL discovery",
        extension_override="wadl",
    )
    wadl_matches = write_wadl_matches(wadl_data.decode("utf-8", "replace")) if wadl_data else []
    write_json(RAW_DIR / "wadl_employees_matches.json", wadl_matches)

    _, presets_data, presets_records, _ = fetch_get(
        client,
        manifest,
        endpoint="/v2/reports/olap/presets",
        raw_name="olap_presets",
        note="filter staff/revenue presets",
    )
    presets_staff = parse_presets_staff(presets_data)
    write_json(RAW_DIR / "olap_presets_staff_matches.json", presets_staff)
    samples["/v2/reports/olap/presets"] = presets_records

    for report_type in ("LABOUR", "ATTENDANCE"):
        status, _, records, _ = fetch_get(
            client,
            manifest,
            endpoint="/v2/reports/olap/columns",
            raw_name=f"olap_columns_{report_type.lower()}",
            params={"reportType": report_type},
            mandatory_params="reportType",
            note=f"reportType={report_type}",
        )
        if status == 200:
            samples[f"/v2/reports/olap/columns?reportType={report_type}"] = records

    _, departments_data, departments, _ = fetch_get(
        client,
        manifest,
        endpoint="/corporation/departments",
        raw_name="departments",
        params={"includeDeleted": "true"},
        mandatory_params="includeDeleted optional",
        note="department lookup for active Черникова",
    )
    active_department = find_active_department(departments)

    _, roles_data, role_rows, _ = fetch_get(
        client,
        manifest,
        endpoint="/employees/roles",
        raw_name="employee_roles",
        params={"includeDeleted": "true"},
        mandatory_params="includeDeleted optional",
        note="role dictionary",
    )
    roles = {clean_text(row.get("id")): row for row in role_rows}
    samples["/employees/roles"] = role_rows

    _, types_data, attendance_type_rows, _ = fetch_get(
        client,
        manifest,
        endpoint="/employees/attendance/types",
        raw_name="attendance_types",
        params={"includeDeleted": "true"},
        mandatory_params="includeDeleted optional",
        note="attendance type dictionary",
    )
    attendance_types = {clean_text(row.get("id")): row for row in attendance_type_rows}
    samples["/employees/attendance/types"] = attendance_type_rows

    _, _, schedule_type_rows, _ = fetch_get(
        client,
        manifest,
        endpoint="/employees/schedule/types",
        raw_name="schedule_types",
        note="schedule type dictionary",
    )
    samples["/employees/schedule/types"] = schedule_type_rows

    _, _, salary_rows, _ = fetch_get(
        client,
        manifest,
        endpoint="/employees/salary",
        raw_name="employees_salary",
        note="current salary/payment settings",
    )
    samples["/employees/salary"] = salary_rows

    _, _, employee_rows, _ = fetch_get(
        client,
        manifest,
        endpoint="/employees",
        raw_name="employees",
        params={"includeDeleted": "true"},
        mandatory_params="includeDeleted optional",
        note="employee lookup; raw may contain PII",
    )
    samples["/employees"] = employee_rows

    pilot_start = dt.date(2026, 4, 1)
    pilot_end = dt.date(2026, 4, 7)
    pilot_attempts = [
        (
            ATTENDANCE_ENDPOINT,
            "pilot_attendance_2026-04-01_2026-04-07_iso",
            {"withPaymentDetails": "false", "from": iso(pilot_start), "to": iso(pilot_end)},
            "yyyy-MM-dd",
            "from,to",
            "pilot no department",
        ),
        (
            ATTENDANCE_ENDPOINT,
            "pilot_attendance_2026-04-01_2026-04-07_ddmmyyyy",
            {"withPaymentDetails": "false", "from": pilot_start.strftime("%d.%m.%Y"), "to": pilot_end.strftime("%d.%m.%Y")},
            "dd.MM.yyyy",
            "from,to",
            "pilot alternate date format",
        ),
        (
            SCHEDULE_ENDPOINT,
            "pilot_schedule_2026-04-01_2026-04-07_iso",
            {"withPaymentDetails": "false", "from": iso(pilot_start), "to": iso(pilot_end)},
            "yyyy-MM-dd",
            "from,to",
            "pilot no department",
        ),
        (
            SCHEDULE_ENDPOINT,
            "pilot_schedule_2026-04-01_2026-04-07_ddmmyyyy",
            {"withPaymentDetails": "false", "from": pilot_start.strftime("%d.%m.%Y"), "to": pilot_end.strftime("%d.%m.%Y")},
            "dd.MM.yyyy",
            "from,to",
            "pilot alternate date format",
        ),
        (
            "/v2/payrolls/list",
            "pilot_v2_payrolls_list_2026-04-01_2026-04-07",
            {
                "dateFrom": iso(pilot_start),
                "dateTo": iso(pilot_end),
                "department": active_department["id"],
                "includeDeleted": "true",
            },
            "yyyy-MM-dd",
            "dateFrom,dateTo,department",
            "payroll list pilot by active department",
        ),
        (
            "/v2/entities/periodSchedules",
            "period_schedules",
            {"includeDeleted": "true"},
            "",
            "none",
            "period schedule dictionary",
        ),
        (
            "/v2/entities/scheduleTypes",
            "v2_schedule_types",
            {"includeDeleted": "true"},
            "",
            "none",
            "v2 schedule type dictionary",
        ),
        (
            "/employees/availability/list",
            "employees_availability_list",
            {},
            "",
            "none",
            "availability endpoint",
        ),
        (
            "/rmsSettings/getEmployees",
            "rms_settings_get_employees",
            {},
            "",
            "none",
            "legacy employee lookup",
        ),
    ]

    if active_department["id"]:
        pilot_attempts.extend(
            [
                (
                    f"{ATTENDANCE_ENDPOINT}/byDepartment/{active_department['id']}",
                    "pilot_attendance_byDepartment_2026-04-01_2026-04-07_iso",
                    {"withPaymentDetails": "false", "from": iso(pilot_start), "to": iso(pilot_end)},
                    "yyyy-MM-dd",
                    "departmentId,from,to",
                    "pilot active department path",
                ),
                (
                    f"{ATTENDANCE_ENDPOINT}/department/{active_department['id']}",
                    "pilot_attendance_department_2026-04-01_2026-04-07_iso",
                    {"withPaymentDetails": "false", "from": iso(pilot_start), "to": iso(pilot_end)},
                    "yyyy-MM-dd",
                    "departmentId,from,to",
                    "pilot active department alternate path",
                ),
                (
                    f"{SCHEDULE_ENDPOINT}/byDepartment/{active_department['id']}",
                    "pilot_schedule_byDepartment_2026-04-01_2026-04-07_iso",
                    {"withPaymentDetails": "false", "from": iso(pilot_start), "to": iso(pilot_end)},
                    "yyyy-MM-dd",
                    "departmentId,from,to",
                    "pilot active department path",
                ),
                (
                    f"/employees/byDepartment/{active_department['id']}",
                    "employees_byDepartment_active",
                    {"includeDeleted": "true"},
                    "",
                    "departmentId",
                    "employee lookup by active department; raw may contain PII",
                ),
            ]
        )

    pilot_attendance_rows: list[dict[str, Any]] = []
    for endpoint, raw_name, params, date_format, mandatory, note in pilot_attempts:
        _, _, records, _ = fetch_get(
            client,
            manifest,
            endpoint=endpoint,
            raw_name=raw_name,
            params=params,
            date_format=date_format,
            mandatory_params=mandatory,
            note=note,
        )
        if endpoint == ATTENDANCE_ENDPOINT and date_format == "yyyy-MM-dd":
            pilot_attendance_rows = records

    sample_employee = first_employee_id(pilot_attendance_rows)
    if sample_employee:
        employee_specific = [
            (
                f"{ATTENDANCE_ENDPOINT}/byEmployee/{sample_employee}",
                "pilot_attendance_byEmployee_2026-04-01_2026-04-07_iso",
                "employeeId,from,to",
                "pilot employee path",
            ),
            (
                f"{SCHEDULE_ENDPOINT}/byEmployee/{sample_employee}",
                "pilot_schedule_byEmployee_2026-04-01_2026-04-07_iso",
                "employeeId,from,to",
                "pilot employee path",
            ),
            (
                f"/employees/salary/byId/{sample_employee}",
                "pilot_salary_byEmployee",
                "employeeId",
                "current salary by employee",
            ),
            (
                f"/employees/salary/byId/{sample_employee}/2026-04-01",
                "pilot_salary_byEmployee_at_2026-04-01",
                "employeeId,date",
                "salary by employee at date",
            ),
        ]
        if active_department["id"]:
            employee_specific.append(
                (
                    f"{ATTENDANCE_ENDPOINT}/byDepartment/{active_department['id']}/byEmployee/{sample_employee}",
                    "pilot_attendance_byDepartment_byEmployee_2026-04-01_2026-04-07_iso",
                    "departmentId,employeeId,from,to",
                    "pilot active department + employee path",
                )
            )
        for endpoint, raw_name, mandatory, note in employee_specific:
            params = {"withPaymentDetails": "false", "from": iso(pilot_start), "to": iso(pilot_end)}
            if "/salary/" in endpoint:
                params = {}
            fetch_get(
                client,
                manifest,
                endpoint=endpoint,
                raw_name=raw_name,
                params=params,
                date_format="yyyy-MM-dd" if "from,to" in mandatory else "",
                mandatory_params=mandatory,
                note=note,
            )

    periods = month_chunks(dt.date(2025, 11, 1), end_date)
    attendance_by_month: dict[str, list[dict[str, Any]]] = {}
    schedule_by_month: dict[str, list[dict[str, Any]]] = {}
    for start, end in periods:
        month = start.strftime("%Y-%m")
        _, _, attendance_rows, _ = fetch_get(
            client,
            manifest,
            endpoint=ATTENDANCE_ENDPOINT,
            raw_name=f"attendance_{month}",
            params={"withPaymentDetails": "false", "from": iso(start), "to": iso(end)},
            period=(start, end),
            date_format="yyyy-MM-dd",
            mandatory_params="from,to",
            note="target monthly attendance export",
        )
        attendance_by_month[month] = attendance_rows
        _, _, schedule_rows, _ = fetch_get(
            client,
            manifest,
            endpoint=SCHEDULE_ENDPOINT,
            raw_name=f"schedule_{month}",
            params={"withPaymentDetails": "false", "from": iso(start), "to": iso(end)},
            period=(start, end),
            date_format="yyyy-MM-dd",
            mandatory_params="from,to",
            note="target monthly schedule check",
        )
        schedule_by_month[month] = schedule_rows

    monthly_rows, stats = build_attendance_monthly(
        attendance_by_month,
        schedule_by_month,
        roles,
        attendance_types,
        periods,
    )
    write_csv(
        PROCESSED_DIR / "attendance_monthly.csv",
        monthly_rows,
        [
            "month",
            "employee_id_hash",
            "role_code",
            "department",
            "planned_minutes",
            "fact_minutes",
            "attendance_type_count_breakdown",
            "status",
            "source_endpoint",
        ],
    )

    target_attendance_rows = [row for rows in attendance_by_month.values() for row in rows]
    target_schedule_rows = [row for rows in schedule_by_month.values() for row in rows]
    samples[ATTENDANCE_ENDPOINT] = target_attendance_rows
    samples[SCHEDULE_ENDPOINT] = target_schedule_rows

    schema_rows = build_field_schema(samples)
    write_csv(
        PROCESSED_DIR / "attendance_field_schema.csv",
        schema_rows,
        ["endpoint", "field_path", "type", "nullable", "example_redacted", "business_meaning"],
    )
    write_inventory(manifest)
    write_report(
        manifest=manifest,
        monthly_rows=monthly_rows,
        stats=stats,
        presets_staff=presets_staff,
        generated_at=generated_at,
        end_date=end_date,
    )
    write_json(RAW_DIR / "manifest.json", manifest)
    print(f"wrote {len(monthly_rows)} attendance aggregate rows")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end-date", default=dt.date.today().isoformat(), help="Inclusive export end date, YYYY-MM-DD")
    args = parser.parse_args()
    end_date = dt.datetime.strptime(args.end_date, "%Y-%m-%d").date()
    run(end_date)


if __name__ == "__main__":
    main()

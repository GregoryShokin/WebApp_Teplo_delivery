#!/usr/bin/env python3
"""Read-only export of the iiko P&L-by-warehouses preset.

The script performs only GET requests against the preset endpoint. The shared
IikoClient may call POST /auth if the configured token is missing or expired.
Secrets are loaded from .env/ENV and are never printed or written to outputs.
"""

from __future__ import annotations

import calendar
import csv
import datetime as dt
import json
import re
import sys
import time
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from export_orders_delivery import (
    IikoClient,
    IikoHTTPError,
    PROJECT_ROOT,
    clean_text,
    iso,
    load_local_env,
    rel,
    save_raw_response,
    write_json,
)


PRESET_ID = "8c13763a-35bf-9f27-017f-5468b1e70021"
ENDPOINT = f"/v2/reports/olap/byPresetId/{PRESET_ID}"
RAW_DIR = PROJECT_ROOT / "research/raw/iiko/pnl"
PROCESSED_DIR = PROJECT_ROOT / "research/processed/economic_block"
OUTPUT_CSV = PROCESSED_DIR / "iiko_pnl_by_preset_rows.csv"
OUTPUT_REPORT = PROCESSED_DIR / "iiko_pnl_export_report.md"

PERIODS: list[tuple[dt.date, dt.date]] = [
    (dt.date(2025, 11, 1), dt.date(2025, 11, 30)),
    (dt.date(2025, 12, 1), dt.date(2025, 12, 31)),
    (dt.date(2026, 1, 1), dt.date(2026, 1, 31)),
    (dt.date(2026, 2, 1), dt.date(2026, 2, 28)),
    (dt.date(2026, 3, 1), dt.date(2026, 3, 31)),
    (dt.date(2026, 4, 1), dt.date(2026, 4, 30)),
    (dt.date(2026, 5, 1), dt.date(2026, 5, 17)),
]

CSV_FIELDS = [
    "period",
    "period_start",
    "period_end",
    "account_type",
    "account_name",
    "store",
    "amount",
]


def period_label(start: dt.date, end: dt.date) -> str:
    last_day = calendar.monthrange(start.year, start.month)[1]
    full_month = start.day == 1 and end == dt.date(start.year, start.month, last_day)
    if full_month:
        return f"{start.year:04d}-{start.month:02d}"
    return f"{start.isoformat()}_{end.isoformat()}"


def money_decimal(value: Any) -> Decimal:
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


def amount_text(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def format_money(value: Decimal) -> str:
    rounded = value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{int(rounded):,}".replace(",", " ")


def sanitize_error(value: str) -> str:
    text = clean_text(value)
    text = re.sub(r"([?&](?:key|pass|password|login)=)[^&\s\"']+", r"\1<redacted>", text)
    text = re.sub(r"\b[0-9a-f]{40}\b", "<sha1-redacted>", text, flags=re.I)
    return text[:1000]


def rows_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return [row for row in payload["data"] if isinstance(row, dict)]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def fetch_period(
    client: IikoClient,
    manifest: list[dict[str, Any]],
    start: dt.date,
    end: dt.date,
) -> list[dict[str, Any]]:
    # iiko byPresetId treats dateTo as an exclusive upper bound.
    exclusive_end = end + dt.timedelta(days=1)
    params = {
        "summary": "false",
        "dateFrom": start.isoformat(),
        "dateTo": exclusive_end.isoformat(),
    }
    status, data = client.request(ENDPOINT, params=params)
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unexpected non-JSON response for {start}..{end}") from exc

    raw_path = RAW_DIR / f"iiko_pnl_by_preset_{start.isoformat()}_{end.isoformat()}.json"
    rows = save_raw_response(
        raw_path,
        data,
        manifest,
        endpoint=ENDPOINT,
        period=(start, end),
        status=status,
        expected_fields=["Account.Type", "Account.Name", "Store", "Sum.ResignedSum"],
        note=f"preset=P&L по складам; reportType=TRANSACTIONS; summary=false; dateTo exclusive={exclusive_end.isoformat()}",
    )
    if not rows:
        rows = rows_from_payload(payload)
        if manifest:
            manifest[-1]["parsed_rows"] = len(rows)
    for row in rows:
        row["_period"] = period_label(start, end)
        row["_period_start"] = start.isoformat()
        row["_period_end"] = end.isoformat()
        row["_source_file"] = rel(raw_path)
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def normalize_rows(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in raw_rows:
        output.append(
            {
                "period": row["_period"],
                "period_start": row["_period_start"],
                "period_end": row["_period_end"],
                "account_type": clean_text(row.get("Account.Type")),
                "account_name": clean_text(row.get("Account.Name")),
                "store": clean_text(row.get("Store")),
                "amount": amount_text(money_decimal(row.get("Sum.ResignedSum"))),
            }
        )
    output.sort(
        key=lambda row: (
            row["period_start"],
            row["account_type"],
            row["account_name"],
            row["store"],
        )
    )
    return output


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    courier_by_period: dict[str, Decimal] = defaultdict(Decimal)
    totals_by_period: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    rows_by_period: dict[str, int] = defaultdict(int)
    stores_by_period: dict[str, set[str]] = defaultdict(set)
    historical_rows: list[dict[str, Any]] = []

    for row in rows:
        amount = money_decimal(row["amount"])
        rows_by_period[row["period"]] += 1
        stores_by_period[row["period"]].add(row["store"] or "(пусто)")
        totals_by_period[(row["period"], row["account_type"])] += amount
        if row["account_name"] == "Зарплата курьеров":
            courier_by_period[row["period"]] += amount
        if "гагарин" in row["store"].casefold():
            historical_rows.append(row)

    return {
        "courier_by_period": courier_by_period,
        "totals_by_period": totals_by_period,
        "rows_by_period": rows_by_period,
        "stores_by_period": stores_by_period,
        "historical_rows": historical_rows,
    }


def write_report(rows: list[dict[str, Any]], manifest: list[dict[str, Any]]) -> None:
    stats = aggregate(rows)
    labels = [period_label(start, end) for start, end in PERIODS]
    raw_files = [
        entry["file"]
        for entry in manifest
        if entry.get("endpoint") == ENDPOINT and not str(entry.get("file", "")).endswith("_error.json")
    ]

    lines: list[str] = [
        "# iiko P&L preset export",
        "",
        "Дата выгрузки: 2026-05-18.",
        "",
        "Режим: read-only. Выполнены только GET-запросы к `byPresetId`; авторизация через `POST /auth` возможна только при истекшем токене. Google Sheets, iiko-данные и настройки не изменялись.",
        "",
        "Контур: активная экономика — Foodmarket Тепло Черникова. Гагарина после января 2024 считается исторической и не смешивается с текущей экономикой.",
        "",
        "Источник: iiko preset `P&L по складам`, reportType `TRANSACTIONS`, endpoint `/resto/api/v2/reports/olap/byPresetId/8c13763a-35bf-9f27-017f-5468b1e70021`.",
        "",
        "Поля нормализации: `Account.Type`, `Account.Name`, `Store`, `Sum.ResignedSum`.",
        "",
        "## Файлы",
        "",
        f"- Raw JSON: `research/raw/iiko/pnl/`, файлов: {len(raw_files)}.",
        "- Processed CSV: `research/processed/economic_block/iiko_pnl_by_preset_rows.csv`.",
        "- Отчет: `research/processed/economic_block/iiko_pnl_export_report.md`.",
        "",
        "## Статус периодов",
        "",
        "| Период | Даты | Raw rows | Stores |",
        "| --- | --- | ---: | --- |",
    ]

    for start, end in PERIODS:
        label = period_label(start, end)
        stores = ", ".join(sorted(stats["stores_by_period"].get(label, set()))) or "-"
        lines.append(
            f"| {label} | {start.isoformat()} — {end.isoformat()} | {stats['rows_by_period'].get(label, 0)} | {stores} |"
        )

    lines.extend(
        [
            "",
            "## Зарплата курьеров",
            "",
            "| Период | Зарплата курьеров |",
            "| --- | ---: |",
        ]
    )
    for label in labels:
        value = stats["courier_by_period"].get(label, Decimal("0"))
        lines.append(f"| {label} | {format_money(value)} |")

    lines.extend(
        [
            "",
            "## Totals By Account Type",
            "",
            "| Период | Account.Type | Sum.ResignedSum |",
            "| --- | --- | ---: |",
        ]
    )
    for label in labels:
        account_types = sorted(
            account_type
            for period, account_type in stats["totals_by_period"]
            if period == label
        )
        for account_type in account_types:
            value = stats["totals_by_period"][(label, account_type)]
            lines.append(f"| {label} | {account_type} | {format_money(value)} |")

    historical_count = len(stats["historical_rows"])
    lines.extend(
        [
            "",
            "## Контроль Гагарина",
            "",
            f"Строк со store, содержащим `Гагарин`: {historical_count}.",
        ]
    )
    if historical_count:
        lines.append("Эти строки оставлены только как размеченные raw/row-level данные и не должны суммироваться с активным контуром Черникова.")
    else:
        lines.append("В нормализованной выгрузке не обнаружены строки складов Гагарина.")

    lines.extend(
        [
            "",
            "## Raw Files",
            "",
        ]
    )
    for raw_file in raw_files:
        lines.append(f"- `{raw_file}`")

    OUTPUT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    load_local_env()
    client = IikoClient()
    manifest: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []

    for start, end in PERIODS:
        try:
            rows = fetch_period(client, manifest, start, end)
        except (IikoHTTPError, TimeoutError, OSError, RuntimeError) as exc:
            error_payload = {
                "endpoint": ENDPOINT,
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
                "message": sanitize_error(getattr(exc, "message", str(exc))),
            }
            if isinstance(exc, IikoHTTPError):
                error_payload["status"] = exc.status
            error_path = RAW_DIR / f"iiko_pnl_by_preset_{start.isoformat()}_{end.isoformat()}_error.json"
            write_json(error_path, error_payload)
            manifest.append(
                {
                    "endpoint": ENDPOINT,
                    "file": rel(error_path),
                    "status": error_payload.get("status", ""),
                    "bytes": 0,
                    "parsed_rows": 0,
                    "period_start": start.isoformat(),
                    "period_end": end.isoformat(),
                    "note": "error",
                }
            )
            write_json(RAW_DIR / "manifest.json", manifest)
            print(f"failed {start.isoformat()}..{end.isoformat()}; see sanitized error JSON")
            return 1

        all_rows.extend(rows)
        print(f"fetched {start.isoformat()}..{end.isoformat()}: {len(rows)} rows")
        time.sleep(0.2)

    normalized = normalize_rows(all_rows)
    write_csv(OUTPUT_CSV, normalized)
    write_report(normalized, manifest)
    write_json(RAW_DIR / "manifest.json", manifest)
    print(f"wrote {rel(OUTPUT_CSV)}: {len(normalized)} rows")
    print(f"wrote {rel(OUTPUT_REPORT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Build sanitized T-Bank aggregates from raw statement JSON files."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import re
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = PROJECT_ROOT / "research/private/tbank"
PROCESSED_DIR = PROJECT_ROOT / "research/processed/tbank"


Money = Decimal


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def parse_date(value: str) -> dt.date:
    return dt.datetime.strptime(value, "%Y-%m-%d").date()


def money(value: Any) -> Money:
    if isinstance(value, dict):
        for key in ("amount", "value", "sum", "rub", "minorUnits"):
            if key in value:
                return money(value.get(key))
        units = value.get("units")
        nano = value.get("nano")
        if units is not None or nano is not None:
            return money(units or 0) + (money(nano or 0) / Decimal("1000000000"))
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, str):
        value = value.replace(" ", "").replace(",", ".")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def fmt_money(value: Money) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def nested_get(payload: Any, path: tuple[str, ...]) -> Any:
    current = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def operation_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    candidates = [
        payload.get("operations"),
        payload.get("transactions"),
        payload.get("items"),
        nested_get(payload, ("statement", "operations")),
        nested_get(payload, ("result", "operations")),
        nested_get(payload, ("data", "operations")),
        nested_get(payload, ("payload", "operations")),
    ]
    for value in candidates:
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def all_payload_files(raw_dir: Path, pattern: str) -> list[Path]:
    files = []
    for path in sorted(raw_dir.glob(pattern)):
        if not path.is_file():
            continue
        if path.name.endswith("_error.json") or "_manifest_" in path.name:
            continue
        files.append(path)
    return files


def scalar_field(row: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = row.get(name)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return str(value)
    return ""


def nested_scalar(row: dict[str, Any], block_name: str, names: tuple[str, ...]) -> str:
    block = row.get(block_name)
    if not isinstance(block, dict):
        return ""
    for name in names:
        value = block.get(name)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return str(value)
    return ""


def clean_digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def inn_kind(value: str) -> str:
    digits = clean_digits(value)
    if len(digits) == 10:
        return "legal_10"
    if len(digits) == 12:
        return "person_or_ip_12"
    if digits:
        return f"other_{len(digits)}"
    return "missing"


def inn_mask(value: str) -> str:
    digits = clean_digits(value)
    if len(digits) < 4:
        return ""
    return f"{'*' * (len(digits) - 4)}{digits[-4:]}"


def counterparty_hash(parts: tuple[str, str, str]) -> str:
    raw = "|".join(parts).casefold().strip()
    if not raw:
        raw = "missing"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def category(row: dict[str, Any]) -> str:
    return scalar_field(row, ("category", "operationCategory", "operationCategoryName", "type")) or "missing"


def operation_date(row: dict[str, Any], fallback: dt.date | None = None) -> str:
    value = scalar_field(
        row,
        (
            "operationDate",
            "transactionDate",
            "date",
            "paymentDate",
            "documentDate",
            "createdAt",
            "updatedAt",
        ),
    )
    match = re.search(r"\d{4}-\d{2}-\d{2}", value)
    if match:
        return match.group(0)
    return fallback.isoformat() if fallback else ""


def date_in_range(day: str, start: dt.date | None, end: dt.date | None) -> bool:
    if not day:
        return True
    try:
        parsed = parse_date(day)
    except ValueError:
        return True
    if start and parsed < start:
        return False
    if end and parsed > end:
        return False
    return True


def amount(row: dict[str, Any]) -> Money:
    for name in (
        "amount",
        "operationAmount",
        "transactionAmount",
        "paymentAmount",
        "amountRub",
        "sum",
        "rubleAmount",
    ):
        value = row.get(name)
        if value not in (None, ""):
            parsed = money(value)
            if parsed != 0:
                return abs(parsed)
    return Decimal("0")


def counterparty_block(row: dict[str, Any], direction_value: str) -> tuple[str, dict[str, Any]]:
    payer = row.get("payer") if isinstance(row.get("payer"), dict) else {}
    receiver = row.get("receiver") if isinstance(row.get("receiver"), dict) else {}
    if direction_value == "outgoing":
        return "receiver", receiver
    if direction_value == "incoming":
        return "payer", payer
    if receiver:
        return "receiver", receiver
    if payer:
        return "payer", payer
    return "missing", {}


def block_value(block: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = block.get(name)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return str(value)
    return ""


def direction(row: dict[str, Any]) -> str:
    value = scalar_field(
        row,
        ("direction", "typeOfOperation", "operationType", "type", "movementType", "debitCreditIndicator"),
    ).casefold()
    outgoing_values = {"debit", "out", "outcome", "expense", "withdrawal", "spending", "расход"}
    incoming_values = {"credit", "in", "income", "receipt", "replenishment", "приход"}
    if value in outgoing_values or "debit" in value or "out" in value or "расход" in value:
        return "outgoing"
    if value in incoming_values or "credit" in value or "income" in value or "приход" in value:
        return "incoming"
    signed_amount = Decimal("0")
    for name in ("amount", "operationAmount", "transactionAmount", "sum"):
        raw = row.get(name)
        if raw not in (None, ""):
            signed_amount = money(raw)
            break
    if signed_amount < 0:
        return "outgoing"
    if signed_amount > 0:
        return "incoming"
    return "unknown"


def counterparty_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    dir_value = direction(row)
    role, block = counterparty_block(row, dir_value)
    inn = clean_digits(block_value(block, ("inn", "INN", "taxId", "tin")))
    account = clean_digits(block_value(block, ("acct", "account", "accountNumber", "bankAccount")))
    name = " ".join(block_value(block, ("name", "fullName", "shortName", "counterpartyName")).split())
    if not (inn or account or name):
        inn = clean_digits(
            nested_scalar(row, "counterparty", ("inn", "taxId", "tin"))
            or scalar_field(row, ("counterpartyInn", "inn"))
        )
        account = clean_digits(
            nested_scalar(row, "counterparty", ("acct", "account", "accountNumber", "bankAccount"))
            or scalar_field(row, ("counterpartyAccount", "accountNumber"))
        )
        name = " ".join(
            (
                nested_scalar(row, "counterparty", ("name", "fullName", "shortName"))
                or scalar_field(row, ("counterpartyName", "counteragentName"))
            ).split()
        )
        role = "counterparty"
    return role, inn, account, name


def collect_field_sets(payloads: list[Any], operations: list[dict[str, Any]]) -> dict[str, list[str]]:
    top_level: set[str] = set()
    operation_fields: set[str] = set()
    payer_fields: set[str] = set()
    receiver_fields: set[str] = set()
    balance_fields: set[str] = set()

    for payload in payloads:
        if isinstance(payload, dict):
            top_level.update(payload.keys())
            for key in payload:
                if "balance" in key.casefold():
                    balance_fields.add(key)
    for row in operations:
        operation_fields.update(row.keys())
        for key in row:
            if "balance" in key.casefold():
                balance_fields.add(f"operation.{key}")
        payer = row.get("payer")
        if isinstance(payer, dict):
            payer_fields.update(payer.keys())
        receiver = row.get("receiver")
        if isinstance(receiver, dict):
            receiver_fields.update(receiver.keys())

    return {
        "top_level": sorted(top_level),
        "operation": sorted(operation_fields),
        "payer": sorted(payer_fields),
        "receiver": sorted(receiver_fields),
        "balances": sorted(balance_fields),
    }


def date_field_formats(operations: list[dict[str, Any]]) -> list[dict[str, str]]:
    names = (
        "operationDate",
        "transactionDate",
        "date",
        "paymentDate",
        "documentDate",
        "createdAt",
        "updatedAt",
    )
    formats: dict[str, set[str]] = defaultdict(set)
    for row in operations:
        for name in names:
            value = row.get(name)
            if not isinstance(value, str) or not value:
                continue
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
                formats[name].add("ISO 8601 UTC seconds")
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z", value):
                formats[name].add("ISO 8601 UTC fractional")
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                formats[name].add("ISO date")
            elif re.search(r"\d{4}-\d{2}-\d{2}", value):
                formats[name].add("contains ISO date")
            else:
                formats[name].add("other")
    return [
        {"field": name, "observed_formats": "; ".join(sorted(values))}
        for name, values in sorted(formats.items())
    ]


def build(args: argparse.Namespace) -> None:
    raw_dir = Path(args.raw_dir)
    files = all_payload_files(raw_dir, args.pattern)
    if not files:
        raise SystemExit("No raw T-Bank statement JSON files found")

    start = parse_date(args.start_date) if args.start_date else None
    end = parse_date(args.end_date) if args.end_date else None

    payloads: list[Any] = []
    operations: list[dict[str, Any]] = []
    source_files: list[str] = []
    for path in files:
        payload = load_json(path)
        payloads.append(payload)
        rows = operation_rows(payload)
        for row in rows:
            day = operation_date(row)
            if date_in_range(day, start, end):
                enriched = dict(row)
                enriched["_source_file"] = rel(path)
                enriched["_operation_date"] = day
                operations.append(enriched)
        source_files.append(rel(path))

    category_rows = aggregate_categories(operations)
    counterparty_rows = aggregate_counterparties(operations)
    daily_rows = aggregate_daily(operations)
    field_sets = collect_field_sets(payloads, operations)
    formats = date_field_formats(operations)

    write_outputs(category_rows, counterparty_rows, daily_rows)
    write_report(
        operations=operations,
        daily_rows=daily_rows,
        category_rows=category_rows,
        counterparty_rows=counterparty_rows,
        field_sets=field_sets,
        formats=formats,
        source_files=source_files,
    )

    print(f"source_files={len(source_files)}")
    print(f"operations={len(operations)}")
    print(f"processed_dir={rel(PROCESSED_DIR)}")


def aggregate_categories(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in operations:
        groups[category(row)].append(row)

    rows = []
    for cat, items in sorted(groups.items()):
        incoming = sum((amount(row) for row in items if direction(row) == "incoming"), Decimal("0"))
        outgoing = sum((amount(row) for row in items if direction(row) == "outgoing"), Decimal("0"))
        unknown = sum((amount(row) for row in items if direction(row) == "unknown"), Decimal("0"))
        dates = sorted(day for day in (operation_date(row) for row in items) if day)
        rows.append(
            {
                "category": cat,
                "operation_count": len(items),
                "incoming_count": sum(1 for row in items if direction(row) == "incoming"),
                "outgoing_count": sum(1 for row in items if direction(row) == "outgoing"),
                "unknown_direction_count": sum(1 for row in items if direction(row) == "unknown"),
                "incoming_total": fmt_money(incoming),
                "outgoing_total_abs": fmt_money(outgoing),
                "unknown_total_abs": fmt_money(unknown),
                "net_total": fmt_money(incoming - outgoing),
                "first_date": dates[0] if dates else "",
                "last_date": dates[-1] if dates else "",
            }
        )
    return rows


def aggregate_counterparties(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in operations:
        groups[counterparty_key(row)].append(row)

    rows = []
    for key, items in sorted(groups.items(), key=lambda item: counterparty_hash(item[0])):
        role, inn, account, name = key
        cp_hash = counterparty_hash(key)
        incoming = sum((amount(row) for row in items if direction(row) == "incoming"), Decimal("0"))
        outgoing = sum((amount(row) for row in items if direction(row) == "outgoing"), Decimal("0"))
        dates = sorted(day for day in (operation_date(row) for row in items) if day)
        categories = sorted({category(row) for row in items})
        rows.append(
            {
                "counterparty_id": f"TCP_{cp_hash}",
                "counterparty_key_hash": cp_hash,
                "counterparty_role": role,
                "inn_kind": inn_kind(inn),
                "inn_mask": inn_mask(inn),
                "account_present": "yes" if account else "no",
                "name_present": "yes" if name else "no",
                "operation_count": len(items),
                "incoming_count": sum(1 for row in items if direction(row) == "incoming"),
                "outgoing_count": sum(1 for row in items if direction(row) == "outgoing"),
                "incoming_total": fmt_money(incoming),
                "outgoing_total_abs": fmt_money(outgoing),
                "net_total": fmt_money(incoming - outgoing),
                "first_date": dates[0] if dates else "",
                "last_date": dates[-1] if dates else "",
                "categories": "; ".join(categories),
                "mapping_status": "needs_manual_mapping",
            }
        )
    return rows


def aggregate_daily(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in operations:
        groups[operation_date(row) or "missing"].append(row)

    rows = []
    for day, items in sorted(groups.items()):
        incoming = sum((amount(row) for row in items if direction(row) == "incoming"), Decimal("0"))
        outgoing = sum((amount(row) for row in items if direction(row) == "outgoing"), Decimal("0"))
        unknown = sum((amount(row) for row in items if direction(row) == "unknown"), Decimal("0"))
        rows.append(
            {
                "date": day,
                "operation_count": len(items),
                "incoming_count": sum(1 for row in items if direction(row) == "incoming"),
                "outgoing_count": sum(1 for row in items if direction(row) == "outgoing"),
                "unknown_direction_count": sum(1 for row in items if direction(row) == "unknown"),
                "incoming_total": fmt_money(incoming),
                "outgoing_total_abs": fmt_money(outgoing),
                "unknown_total_abs": fmt_money(unknown),
                "net_total": fmt_money(incoming - outgoing),
                "source": "T-Bank API /api/v1/statement",
            }
        )
    return rows


def write_outputs(
    category_rows: list[dict[str, Any]],
    counterparty_rows: list[dict[str, Any]],
    daily_rows: list[dict[str, Any]],
) -> None:
    write_csv(
        PROCESSED_DIR / "operation_categories.csv",
        category_rows,
        [
            "category",
            "operation_count",
            "incoming_count",
            "outgoing_count",
            "unknown_direction_count",
            "incoming_total",
            "outgoing_total_abs",
            "unknown_total_abs",
            "net_total",
            "first_date",
            "last_date",
        ],
    )
    write_csv(
        PROCESSED_DIR / "counterparty_summary.csv",
        counterparty_rows,
        [
            "counterparty_id",
            "counterparty_key_hash",
            "counterparty_role",
            "inn_kind",
            "inn_mask",
            "account_present",
            "name_present",
            "operation_count",
            "incoming_count",
            "outgoing_count",
            "incoming_total",
            "outgoing_total_abs",
            "net_total",
            "first_date",
            "last_date",
            "categories",
            "mapping_status",
        ],
    )
    write_csv(
        PROCESSED_DIR / "cashflow_daily.csv",
        daily_rows,
        [
            "date",
            "operation_count",
            "incoming_count",
            "outgoing_count",
            "unknown_direction_count",
            "incoming_total",
            "outgoing_total_abs",
            "unknown_total_abs",
            "net_total",
            "source",
        ],
    )


def write_report(
    *,
    operations: list[dict[str, Any]],
    daily_rows: list[dict[str, Any]],
    category_rows: list[dict[str, Any]],
    counterparty_rows: list[dict[str, Any]],
    field_sets: dict[str, list[str]],
    formats: list[dict[str, str]],
    source_files: list[str],
) -> None:
    dates = sorted(str(row.get("date")) for row in daily_rows if row.get("date") and row.get("date") != "missing")
    period = f"{dates[0]} - {dates[-1]}" if dates else "unknown"
    incoming = sum((money(row.get("incoming_total")) for row in daily_rows), Decimal("0"))
    outgoing = sum((money(row.get("outgoing_total_abs")) for row in daily_rows), Decimal("0"))
    unknown = sum((money(row.get("unknown_total_abs")) for row in daily_rows), Decimal("0"))
    missing_category = sum(1 for row in operations if category(row) == "missing")
    missing_date = sum(1 for row in operations if not operation_date(row))
    unknown_direction = sum(1 for row in operations if direction(row) == "unknown")

    lines = [
        "# T-Bank API: выписка и безопасные агрегаты",
        "",
        f"Дата сборки: {dt.date.today().isoformat()}.",
        "",
        "Источник: T-Bank Business Open API `GET /api/v1/statement`; raw хранится только в `research/private/tbank/`.",
        "В processed-файлы не перенесены полные строки выписки, полные счета, назначения платежей, ФИО и полные ИНН.",
        "",
        "## Короткий ответ",
        "",
        f"- Период операций: `{period}`.",
        f"- Операций: {len(operations)}.",
        f"- Поступления всего: {fmt_money(incoming)} руб.",
        f"- Списания всего: {fmt_money(outgoing)} руб.",
        f"- Net cashflow: {fmt_money(incoming - outgoing)} руб.",
        f"- Сумма с нераспознанным направлением: {fmt_money(unknown)} руб.",
        "",
        "## Выгруженные файлы",
        "",
        "- `research/processed/tbank/operation_categories.csv`",
        "- `research/processed/tbank/counterparty_summary.csv`",
        "- `research/processed/tbank/cashflow_daily.csv`",
        "",
        "## Структура ответа",
        "",
        f"- Top-level поля: `{', '.join(field_sets.get('top_level', [])) or 'не найдены'}`.",
        f"- Поля операции: `{', '.join(field_sets.get('operation', [])) or 'не найдены'}`.",
        f"- Поля `payer`: `{', '.join(field_sets.get('payer', [])) or 'не найдены'}`.",
        f"- Поля `receiver`: `{', '.join(field_sets.get('receiver', [])) or 'не найдены'}`.",
        f"- Поля балансов: `{', '.join(field_sets.get('balances', [])) or 'не найдены'}`.",
        "",
        "## Форматы дат",
        "",
    ]
    if formats:
        lines.extend(["| Поле | Наблюдаемый формат |", "| --- | --- |"])
        for row in formats:
            lines.append(f"| `{row['field']}` | {row['observed_formats']} |")
        lines.append("")
    else:
        lines.extend(["Дата-поля в операциях не найдены.", ""])

    lines.extend(
        [
            "## Edge cases",
            "",
            f"- Операций без категории: {missing_category}.",
            f"- Операций без распознанной даты: {missing_date}.",
            f"- Операций с нераспознанным направлением: {unknown_direction}.",
            "- Входящие движения T-Bank помечены как `требует проверки`: не считать их выручкой без сверки со Sber/iiko.",
            f"- Категорий: {len(category_rows)}.",
            f"- Псевдонимов контрагентов: {len(counterparty_rows)}.",
            "",
            "## Raw-источники",
            "",
        ]
    )
    lines.extend(f"- `{path}`" for path in source_files)
    lines.append("")

    write_text(PROCESSED_DIR / "report.md", "\n".join(lines))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build sanitized T-Bank cash-flow aggregates")
    parser.add_argument("--raw-dir", default=str(RAW_DIR))
    parser.add_argument("--pattern", default="statement_*.json")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    build(parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

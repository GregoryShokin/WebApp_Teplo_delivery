#!/usr/bin/env python3
"""Build operation-level tables from Sber statement raw JSON.

The public table masks counterparties and omits payment purposes. The private
table keeps full operation details for owner review and must stay in
research/private/.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = PROJECT_ROOT / "integrations/sber/private/statement"
PROCESSED_DIR = PROJECT_ROOT / "research/processed/sber"
PRIVATE_DIR = PROJECT_ROOT / "integrations/sber/private/processed"


def parse_date(value: str) -> dt.date:
    return dt.datetime.strptime(value, "%Y-%m-%d").date()


def date_range(start: dt.date, end: dt.date) -> list[dt.date]:
    if end < start:
        raise SystemExit("--end-date must be greater than or equal to --start-date")
    return [start + dt.timedelta(days=offset) for offset in range((end - start).days + 1)]


def money(value: Any) -> Decimal:
    if isinstance(value, dict):
        value = value.get("amount")
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def fmt_money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def fmt_rate(numerator: Decimal, denominator: Decimal) -> str:
    if not denominator:
        return "0.00"
    return str((numerator / denominator * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


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


def direction(row: dict[str, Any]) -> str:
    value = str(row.get("direction") or "").upper()
    if value in {"CREDIT", "DEBIT"}:
        return value
    return "UNKNOWN"


def amount_rub(row: dict[str, Any]) -> Decimal:
    return money(row.get("amountRub") or row.get("amount"))


def clean_text(value: str) -> str:
    return " ".join(str(value or "").split())


def mask_inn(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) < 4:
        return ""
    return f"{'*' * (len(digits) - 4)}{digits[-4:]}"


def mask_account(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) < 8:
        return ""
    return f"{digits[:4]}...{digits[-4:]}"


def counterparty_fields(row: dict[str, Any]) -> dict[str, str]:
    transfer = row.get("rurTransfer")
    if not isinstance(transfer, dict):
        return {
            "name": "",
            "inn": "",
            "account": str(row.get("correspondingAccount") or ""),
            "bank_bic": "",
        }
    if direction(row) == "CREDIT":
        return {
            "name": str(transfer.get("payerName") or ""),
            "inn": str(transfer.get("payerInn") or ""),
            "account": str(transfer.get("payerAccount") or row.get("correspondingAccount") or ""),
            "bank_bic": str(transfer.get("payerBankBic") or ""),
        }
    if direction(row) == "DEBIT":
        return {
            "name": str(transfer.get("payeeName") or ""),
            "inn": str(transfer.get("payeeInn") or ""),
            "account": str(transfer.get("payeeAccount") or row.get("correspondingAccount") or ""),
            "bank_bic": str(transfer.get("payeeBankBic") or ""),
        }
    return {
        "name": "",
        "inn": "",
        "account": str(row.get("correspondingAccount") or ""),
        "bank_bic": "",
    }


def counterparty_key(fields: dict[str, str]) -> tuple[str, str, str]:
    return (
        re.sub(r"\D", "", fields.get("inn", "")),
        re.sub(r"\D", "", fields.get("account", "")),
        clean_text(fields.get("name", "")).casefold(),
    )


def operation_kind(row: dict[str, Any]) -> str:
    text = clean_text(row.get("paymentPurpose") or "").casefold()
    if "эквайр" in text:
        return "acquiring_inflow"
    if "прием платеж" in text or "приём платеж" in text:
        return "sber_payment_contract_inflow"
    if direction(row) == "CREDIT":
        return "other_inflow"
    if "аренд" in text:
        return "rent_outflow"
    if any(word in text for word in ("кредит", "овердрафт", "ссуд")):
        return "financing_outflow"
    return "other_outflow" if direction(row) == "DEBIT" else "unknown"


def parse_decimal_match(pattern: str, text: str) -> Decimal:
    match = re.search(pattern, text, flags=re.I)
    if not match:
        return Decimal("0")
    return money(match.group(1))


def extract_commission(row: dict[str, Any]) -> dict[str, Any]:
    purpose = clean_text(row.get("paymentPurpose") or "")
    commission = Decimal("0")
    vat = Decimal("0")
    source = ""

    direct = parse_decimal_match(r"\bКомиссия\s+([0-9]+(?:[.,][0-9]+)?)", purpose)
    if direct:
        commission = direct
        vat = parse_decimal_match(r"НДС\s+([0-9]+(?:[.,][0-9]+)?)", purpose)
        source = "purpose_commission_with_vat"

    retained = parse_decimal_match(
        r"удержано\s+комисси[ия]\s+за\s+при[её]м\s+платежей\s+([0-9]+(?:[.,][0-9]+)?)",
        purpose,
    )
    if retained:
        commission = retained
        vat = Decimal("0")
        source = "purpose_retained_payment_acceptance_fee"

    merchant_match = re.search(r"Мерчант\s*№\s*([0-9]+)", purpose, flags=re.I)
    merchant_id = merchant_match.group(1) if merchant_match else ""
    return {
        "commission_rub": commission,
        "commission_vat_rub": vat,
        "commission_source": source,
        "merchant_id": merchant_id,
        "gross_amount_estimate": amount_rub(row) + commission,
    }


def load_transactions(day: dt.date) -> list[dict[str, Any]]:
    day_dir = RAW_DIR / day.isoformat()
    rows: list[dict[str, Any]] = []
    for path in sorted(day_dir.glob("transactions_page_*.json")):
        if path.name.endswith("_error.json"):
            continue
        payload = load_json(path)
        transactions = payload.get("transactions") if isinstance(payload, dict) else None
        if isinstance(transactions, list):
            rows.extend(row for row in transactions if isinstance(row, dict))
    return rows


def build_rows(start: dt.date, end: dt.date) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    raw_items: list[dict[str, Any]] = []
    counterparty_keys: set[tuple[str, str, str]] = set()
    for day in date_range(start, end):
        for row in load_transactions(day):
            fields = counterparty_fields(row)
            key = counterparty_key(fields)
            counterparty_keys.add(key)
            raw_items.append(
                {
                    "statement_date": day.isoformat(),
                    "row": row,
                    "fields": fields,
                    "key": key,
                }
            )

    counterparty_ids = {key: f"CP{index:04d}" for index, key in enumerate(sorted(counterparty_keys), start=1)}
    public_rows = []
    private_rows = []
    for index, item in enumerate(raw_items, start=1):
        row = item["row"]
        fields = item["fields"]
        cp_id = counterparty_ids[item["key"]]
        commission = extract_commission(row)
        op_date = str(row.get("operationDate") or row.get("documentDate") or item["statement_date"])
        purpose = clean_text(row.get("paymentPurpose") or "")
        base = {
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "row_no": index,
            "statement_date": item["statement_date"],
            "operation_datetime": op_date,
            "direction": direction(row),
            "operation_kind": operation_kind(row),
            "amount_rub": fmt_money(amount_rub(row)),
            "commission_rub": fmt_money(commission["commission_rub"]),
            "commission_vat_rub": fmt_money(commission["commission_vat_rub"]),
            "gross_amount_estimate": fmt_money(commission["gross_amount_estimate"]),
            "merchant_id": commission["merchant_id"],
            "commission_source": commission["commission_source"],
            "operation_code": row.get("operationCode") or "",
            "counterparty_id": cp_id,
        }
        public_rows.append(
            {
                **base,
                "counterparty_inn_mask": mask_inn(fields.get("inn", "")),
                "counterparty_account_mask": mask_account(fields.get("account", "")),
                "purpose_summary": public_purpose_summary(row, commission),
            }
        )
        private_rows.append(
            {
                **base,
                "counterparty_name": fields.get("name", ""),
                "counterparty_inn": re.sub(r"\D", "", fields.get("inn", "")),
                "counterparty_account": re.sub(r"\D", "", fields.get("account", "")),
                "counterparty_bank_bic": re.sub(r"\D", "", fields.get("bank_bic", "")),
                "operation_id": row.get("operationId") or "",
                "uuid": row.get("uuid") or "",
                "payment_purpose": purpose,
            }
        )
    summary_rows = build_summary(public_rows)
    return public_rows, private_rows, summary_rows


def public_purpose_summary(row: dict[str, Any], commission: dict[str, Any]) -> str:
    kind = operation_kind(row)
    if commission["commission_rub"]:
        if kind == "acquiring_inflow":
            return "Поступление по эквайрингу; комиссия извлечена из назначения платежа"
        if kind == "sber_payment_contract_inflow":
            return "Поступление от ПАО Сбербанк по договору приема платежей; не эквайринг"
        return "Операция с комиссией в назначении платежа"
    if direction(row) == "CREDIT":
        return "Поступление без найденной комиссии в назначении"
    return "Списание без найденной комиссии в назначении"


def build_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(str(row["direction"]), str(row["operation_kind"]))].append(row)
    summary = []
    for (dir_value, kind), items in sorted(groups.items()):
        amount_total = sum((money(item["amount_rub"]) for item in items), Decimal("0"))
        commission_total = sum((money(item["commission_rub"]) for item in items), Decimal("0"))
        vat_total = sum((money(item["commission_vat_rub"]) for item in items), Decimal("0"))
        gross_total = sum((money(item["gross_amount_estimate"]) for item in items), Decimal("0"))
        summary.append(
            {
                "direction": dir_value,
                "operation_kind": kind,
                "operations_count": len(items),
                "amount_rub_total": fmt_money(amount_total),
                "commission_rub_total": fmt_money(commission_total),
                "commission_vat_rub_total": fmt_money(vat_total),
                "gross_amount_estimate_total": fmt_money(gross_total),
                "commission_rate_pct": fmt_rate(commission_total, gross_total),
            }
        )
    return summary


def build_daily_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row["statement_date"])].append(row)
    summary = []
    for day, items in sorted(groups.items()):
        amount_total = sum((money(item["amount_rub"]) for item in items), Decimal("0"))
        commission_total = sum((money(item["commission_rub"]) for item in items), Decimal("0"))
        gross_total = sum((money(item["gross_amount_estimate"]) for item in items), Decimal("0"))
        summary.append(
            {
                "statement_date": day,
                "operations_count": len(items),
                "amount_rub_total": fmt_money(amount_total),
                "commission_rub_total": fmt_money(commission_total),
                "gross_amount_estimate_total": fmt_money(gross_total),
                "commission_rate_pct": fmt_rate(commission_total, gross_total),
            }
        )
    return summary


def build(args: argparse.Namespace) -> None:
    start = parse_date(args.start_date)
    end = parse_date(args.end_date)
    public_rows, private_rows, summary_rows = build_rows(start, end)
    daily_summary_rows = build_daily_summary(public_rows)
    suffix = f"{start.isoformat()}_{end.isoformat()}"
    public_path = PROCESSED_DIR / f"operations_{suffix}.csv"
    private_path = PRIVATE_DIR / f"operations_{suffix}_private.csv"
    summary_path = PROCESSED_DIR / f"operations_{suffix}_summary.csv"
    daily_summary_path = PROCESSED_DIR / f"operations_{suffix}_daily_summary.csv"
    report_path = PROCESSED_DIR / f"operations_{suffix}_report.md"

    public_fields = [
        "period_start",
        "period_end",
        "row_no",
        "statement_date",
        "operation_datetime",
        "direction",
        "operation_kind",
        "amount_rub",
        "commission_rub",
        "commission_vat_rub",
        "gross_amount_estimate",
        "merchant_id",
        "commission_source",
        "operation_code",
        "counterparty_id",
        "counterparty_inn_mask",
        "counterparty_account_mask",
        "purpose_summary",
    ]
    private_fields = [
        *public_fields[:15],
        "counterparty_name",
        "counterparty_inn",
        "counterparty_account",
        "counterparty_bank_bic",
        "operation_id",
        "uuid",
        "payment_purpose",
    ]
    summary_fields = [
        "direction",
        "operation_kind",
        "operations_count",
        "amount_rub_total",
        "commission_rub_total",
        "commission_vat_rub_total",
        "gross_amount_estimate_total",
        "commission_rate_pct",
    ]
    daily_summary_fields = [
        "statement_date",
        "operations_count",
        "amount_rub_total",
        "commission_rub_total",
        "gross_amount_estimate_total",
        "commission_rate_pct",
    ]
    write_csv(public_path, public_rows, public_fields)
    write_csv(private_path, private_rows, private_fields)
    write_csv(summary_path, summary_rows, summary_fields)
    write_csv(daily_summary_path, daily_summary_rows, daily_summary_fields)
    write_report(
        report_path,
        start,
        end,
        public_rows,
        summary_rows,
        daily_summary_rows,
        public_path,
        private_path,
        summary_path,
        daily_summary_path,
    )
    print(f"operations={len(public_rows)}")
    print(f"public={public_path.relative_to(PROJECT_ROOT)}")
    print(f"private={private_path.relative_to(PROJECT_ROOT)}")


def write_report(
    path: Path,
    start: dt.date,
    end: dt.date,
    rows: list[dict[str, Any]],
    summary_rows: list[dict[str, Any]],
    daily_summary_rows: list[dict[str, Any]],
    public_path: Path,
    private_path: Path,
    summary_path: Path,
    daily_summary_path: Path,
) -> None:
    inflow = sum((money(row["amount_rub"]) for row in rows if row["direction"] == "CREDIT"), Decimal("0"))
    outflow = sum((money(row["amount_rub"]) for row in rows if row["direction"] == "DEBIT"), Decimal("0"))
    commission = sum((money(row["commission_rub"]) for row in rows), Decimal("0"))
    vat = sum((money(row["commission_vat_rub"]) for row in rows), Decimal("0"))
    lines = [
        "# Sber API: операции и комиссии приема платежей",
        "",
        f"Период: `{start.isoformat()}` - `{end.isoformat()}`.",
        "",
        f"- Операций: {len(rows)}.",
        f"- Поступления: {fmt_money(inflow)} руб.",
        f"- Списания: {fmt_money(outflow)} руб.",
        f"- Комиссия, найденная внутри назначений платежа: {fmt_money(commission)} руб.",
        f"- НДС в комиссии, где указан: {fmt_money(vat)} руб.",
        "",
        "## Файлы",
        "",
        f"- Публичная таблица: `{public_path.relative_to(PROJECT_ROOT)}`",
        f"- Сводка: `{summary_path.relative_to(PROJECT_ROOT)}`",
        f"- Дневная сводка: `{daily_summary_path.relative_to(PROJECT_ROOT)}`",
        f"- Полная приватная таблица: `{private_path.relative_to(PROJECT_ROOT)}`",
        "",
        "## Сводка по дням",
        "",
        "| Дата | Операций | Нетто-зачисления | Комиссия | Gross estimate | Комиссия % |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in daily_summary_rows:
        lines.append(
            f"| {row['statement_date']} | {row['operations_count']} | {row['amount_rub_total']} | "
            f"{row['commission_rub_total']} | {row['gross_amount_estimate_total']} | {row['commission_rate_pct']}% |"
        )
    lines.extend(
        [
            "",
        "## Сводка по типам операций",
        "",
            "| Направление | Тип | Операций | Сумма | Комиссия | Gross estimate | Комиссия % |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in summary_rows:
        lines.append(
            f"| {row['direction']} | {row['operation_kind']} | {row['operations_count']} | "
            f"{row['amount_rub_total']} | {row['commission_rub_total']} | "
            f"{row['gross_amount_estimate_total']} | {row['commission_rate_pct']}% |"
        )
    lines.extend(
        [
            "",
            "Примечание: `gross_amount_estimate` = сумма зачисления/списания + комиссия, найденная в назначении платежа. Для входящих платежей это оценка оборота до удержания комиссии.",
            "",
        ]
    )
    write_text(path, "\n".join(lines))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Sber operation table with acquiring commissions")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    return parser.parse_args()


def main() -> int:
    build(parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

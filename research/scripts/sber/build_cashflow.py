#!/usr/bin/env python3
"""Build sanitized cash-flow aggregates from raw Sber statement JSON files."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = PROJECT_ROOT / "research/private/sber/statement"
PROCESSED_DIR = PROJECT_ROOT / "research/processed/sber"
PRIVATE_DIR = PROJECT_ROOT / "research/private/sber/processed"


Money = Decimal


def parse_date(value: str) -> dt.date:
    return dt.datetime.strptime(value, "%Y-%m-%d").date()


def date_from_dir(path: Path) -> dt.date | None:
    try:
        return parse_date(path.name)
    except ValueError:
        return None


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def money(value: Any) -> Money:
    if isinstance(value, dict):
        value = value.get("amount")
    if value is None or value == "":
        return Decimal("0")
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


def date_dirs(raw_dir: Path, start: dt.date | None, end: dt.date | None) -> list[Path]:
    dirs = []
    for path in raw_dir.iterdir() if raw_dir.exists() else []:
        if not path.is_dir():
            continue
        day = date_from_dir(path)
        if day is None:
            continue
        if start and day < start:
            continue
        if end and day > end:
            continue
        dirs.append(path)
    return sorted(dirs, key=lambda p: p.name)


def load_transactions(day_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    files: list[str] = []
    for path in sorted(day_dir.glob("transactions_page_*.json")):
        if path.name.endswith("_error.json"):
            continue
        payload = load_json(path)
        files.append(rel(path))
        transactions = payload.get("transactions") if isinstance(payload, dict) else None
        if isinstance(transactions, list):
            rows.extend(row for row in transactions if isinstance(row, dict))
    return rows, files


def summary_numbers(summary: dict[str, Any]) -> dict[str, Money | int]:
    return {
        "opening_balance": money(summary.get("openingBalance")),
        "closing_balance": money(summary.get("closingBalance")),
        "credit_turnover": money(summary.get("creditTurnover")),
        "debit_turnover": money(summary.get("debitTurnover")),
        "credit_transactions": int(summary.get("creditTransactionsNumber") or 0),
        "debit_transactions": int(summary.get("debitTransactionsNumber") or 0),
    }


def amount_rub(row: dict[str, Any]) -> Money:
    return money(row.get("amountRub") or row.get("amount"))


def direction(row: dict[str, Any]) -> str:
    value = str(row.get("direction") or "").upper()
    if value in {"CREDIT", "DEBIT"}:
        return value
    return "UNKNOWN"


def inn_kind(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 10:
        return "legal_10"
    if len(digits) == 12:
        return "person_or_ip_12"
    if digits:
        return f"other_{len(digits)}"
    return "missing"


def inn_mask(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) < 4:
        return ""
    return f"{'*' * (len(digits) - 4)}{digits[-4:]}"


def account_mask(value: str) -> str:
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
        " ".join(fields.get("name", "").split()).casefold(),
    )


def assign_counterparty_ids(keys: set[tuple[str, str, str]]) -> dict[tuple[str, str, str], str]:
    return {key: f"CP{index:04d}" for index, key in enumerate(sorted(keys), start=1)}


def operation_date(row: dict[str, Any], fallback: dt.date) -> str:
    value = str(row.get("operationDate") or row.get("documentDate") or fallback.isoformat())
    return value[:10]


def month_of(day: str) -> str:
    return day[:7]


def build(args: argparse.Namespace) -> None:
    start = parse_date(args.start_date) if args.start_date else None
    end = parse_date(args.end_date) if args.end_date else None
    dirs = date_dirs(Path(args.raw_dir), start, end)
    if not dirs:
        raise SystemExit("No raw statement date directories found")

    daily_rows: list[dict[str, Any]] = []
    all_transactions: list[dict[str, Any]] = []
    counterparty_keys: set[tuple[str, str, str]] = set()

    for day_dir in dirs:
        day = parse_date(day_dir.name)
        summary_path = day_dir / "summary.json"
        if not summary_path.exists():
            daily_rows.append(
                {
                    "date": day.isoformat(),
                    "status": "missing_summary",
                    "source_files": "",
                }
            )
            continue
        summary = load_json(summary_path)
        if not isinstance(summary, dict):
            continue
        numbers = summary_numbers(summary)
        transactions, transaction_files = load_transactions(day_dir)

        credit_total = sum((amount_rub(row) for row in transactions if direction(row) == "CREDIT"), Decimal("0"))
        debit_total = sum((amount_rub(row) for row in transactions if direction(row) == "DEBIT"), Decimal("0"))
        credit_count = sum(1 for row in transactions if direction(row) == "CREDIT")
        debit_count = sum(1 for row in transactions if direction(row) == "DEBIT")
        balance_delta = numbers["closing_balance"] - numbers["opening_balance"] - credit_total + debit_total
        credit_delta = numbers["credit_turnover"] - credit_total
        debit_delta = numbers["debit_turnover"] - debit_total
        count_delta = (
            int(numbers["credit_transactions"])
            + int(numbers["debit_transactions"])
            - credit_count
            - debit_count
        )
        status = "ok"
        if any(abs(value) >= Decimal("0.01") for value in (balance_delta, credit_delta, debit_delta)):
            status = "amount_mismatch"
        if count_delta != 0:
            status = f"{status};count_mismatch"

        daily_rows.append(
            {
                "date": day.isoformat(),
                "opening_balance": fmt_money(numbers["opening_balance"]),
                "inflows_total": fmt_money(credit_total),
                "outflows_total_abs": fmt_money(debit_total),
                "net_cashflow": fmt_money(credit_total - debit_total),
                "closing_balance": fmt_money(numbers["closing_balance"]),
                "summary_credit_turnover": fmt_money(numbers["credit_turnover"]),
                "summary_debit_turnover_abs": fmt_money(numbers["debit_turnover"]),
                "transaction_credit_count": credit_count,
                "transaction_debit_count": debit_count,
                "summary_credit_count": numbers["credit_transactions"],
                "summary_debit_count": numbers["debit_transactions"],
                "transactions_total": credit_count + debit_count,
                "balance_formula_delta": fmt_money(balance_delta),
                "credit_turnover_delta": fmt_money(credit_delta),
                "debit_turnover_delta": fmt_money(debit_delta),
                "transaction_count_delta": count_delta,
                "status": status,
                "source_files": "; ".join([rel(summary_path), *transaction_files]),
            }
        )

        for row in transactions:
            enriched = dict(row)
            enriched["_statement_date"] = day.isoformat()
            enriched["_operation_date"] = operation_date(row, day)
            fields = counterparty_fields(row)
            key = counterparty_key(fields)
            enriched["_counterparty_key"] = key
            enriched["_counterparty_fields"] = fields
            all_transactions.append(enriched)
            counterparty_keys.add(key)

    counterparty_ids = assign_counterparty_ids(counterparty_keys)
    monthly = aggregate_monthly(daily_rows)
    operation_codes = aggregate_operation_codes(all_transactions)
    counterparty_summary, private_map = aggregate_counterparties(all_transactions, counterparty_ids)
    article_rows, private_transaction_rows = aggregate_articles(all_transactions, counterparty_ids)

    write_outputs(
        daily_rows,
        monthly,
        operation_codes,
        counterparty_summary,
        private_map,
        article_rows,
        private_transaction_rows,
    )
    write_report(daily_rows, monthly, operation_codes, counterparty_summary, article_rows)

    print(f"daily_rows={len(daily_rows)}")
    print(f"transactions={len(all_transactions)}")
    print(f"processed_dir={rel(PROCESSED_DIR)}")


def aggregate_monthly(daily_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in daily_rows:
        if row.get("status") == "missing_summary":
            continue
        groups[month_of(str(row["date"]))].append(row)

    rows = []
    for month, items in sorted(groups.items()):
        items = sorted(items, key=lambda row: str(row["date"]))
        inflows = sum((money(row["inflows_total"]) for row in items), Decimal("0"))
        outflows = sum((money(row["outflows_total_abs"]) for row in items), Decimal("0"))
        closing_values = [(str(row["date"]), money(row["closing_balance"])) for row in items]
        min_day, min_balance = min(closing_values, key=lambda item: item[1])
        max_outflow_row = max(items, key=lambda row: money(row["outflows_total_abs"]))
        rows.append(
            {
                "period": month,
                "days_count": len(items),
                "transactions_count": sum(int(row.get("transactions_total") or 0) for row in items),
                "opening_balance_first_day": items[0].get("opening_balance", ""),
                "inflows_total": fmt_money(inflows),
                "outflows_total_abs": fmt_money(outflows),
                "net_cashflow": fmt_money(inflows - outflows),
                "closing_balance_last_day": items[-1].get("closing_balance", ""),
                "min_closing_balance": fmt_money(min_balance),
                "min_closing_balance_date": min_day,
                "max_outflow_day_abs": max_outflow_row.get("outflows_total_abs", ""),
                "max_outflow_day": max_outflow_row.get("date", ""),
                "status": "ok" if all(row.get("status") == "ok" for row in items) else "check_daily_rows",
                "source": "Sber API statement summary + transactions",
            }
        )
    return rows


def aggregate_operation_codes(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in transactions:
        key = (
            month_of(str(row.get("_operation_date") or row.get("_statement_date"))),
            direction(row),
            str(row.get("operationCode") or "missing"),
        )
        groups[key].append(row)

    rows = []
    for (period, dir_value, code), items in sorted(groups.items()):
        amount_total = sum((amount_rub(row) for row in items), Decimal("0"))
        dates = sorted(str(row.get("_operation_date")) for row in items)
        rows.append(
            {
                "period": period,
                "direction": dir_value,
                "operation_code": code,
                "transaction_count": len(items),
                "amount_rub_total_abs": fmt_money(amount_total),
                "first_date": dates[0],
                "last_date": dates[-1],
            }
        )
    return rows


def aggregate_counterparties(
    transactions: list[dict[str, Any]],
    counterparty_ids: dict[tuple[str, str, str], str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in transactions:
        cp_id = counterparty_ids[row["_counterparty_key"]]
        groups[(cp_id, direction(row))].append(row)

    summary_rows = []
    private_rows = []
    seen_private: set[str] = set()
    for (cp_id, dir_value), items in sorted(groups.items()):
        amount_total = sum((amount_rub(row) for row in items), Decimal("0"))
        dates = sorted(str(row.get("_operation_date")) for row in items)
        fields = items[0]["_counterparty_fields"]
        codes = sorted({str(row.get("operationCode") or "missing") for row in items})
        summary_rows.append(
            {
                "counterparty_id": cp_id,
                "direction": dir_value,
                "inn_kind": inn_kind(fields.get("inn", "")),
                "inn_mask": inn_mask(fields.get("inn", "")),
                "transaction_count": len(items),
                "amount_rub_total_abs": fmt_money(amount_total),
                "first_date": dates[0],
                "last_date": dates[-1],
                "operation_codes": "; ".join(codes),
                "mapping_status": "needs_manual_mapping",
            }
        )
        if cp_id not in seen_private:
            private_rows.append(
                {
                    "counterparty_id": cp_id,
                    "name": fields.get("name", ""),
                    "inn": re.sub(r"\D", "", fields.get("inn", "")),
                    "account_mask": account_mask(fields.get("account", "")),
                    "account_number": re.sub(r"\D", "", fields.get("account", "")),
                    "bank_bic": re.sub(r"\D", "", fields.get("bank_bic", "")),
                    "comment": "Private mapping for management classification; do not commit.",
                }
            )
            seen_private.add(cp_id)
    return summary_rows, private_rows


def classify_transaction(row: dict[str, Any]) -> dict[str, str]:
    fields = row.get("_counterparty_fields") or {}
    text = " ".join(
        [
            str(fields.get("name") or ""),
            str(row.get("paymentPurpose") or ""),
            str(row.get("operationCode") or ""),
        ]
    ).casefold()
    dir_value = direction(row)

    if dir_value == "CREDIT":
        if any(needle in text for needle in ("эквайр", "комисс", "агрегатор", "оплат")):
            return {
                "article_group": "operating_inflow",
                "article": "Поступления от эквайринга / агрегаторов",
                "review_status": "needs_owner_review",
            }
        return {
            "article_group": "operating_inflow",
            "article": "Поступления / выручка, требуется разметка",
            "review_status": "needs_owner_review",
        }

    rules = [
        (
            ("овердрафт", "кредит", "ссуд", "основн", "процент"),
            "financing",
            "Кредиты / овердрафт",
        ),
        (
            ("налог", "ндфл", "страхов", "взнос", "пенсион", "фсс", "фомс"),
            "taxes",
            "Налоги и взносы",
        ),
        (
            ("аренд",),
            "operating_outflow",
            "Аренда",
        ),
        (
            ("зарплат", "заработ", "аванс", "депозит", "больнич", "отпуск"),
            "operating_outflow",
            "ФОТ / выплаты персоналу",
        ),
        (
            ("курьер", "достав"),
            "operating_outflow",
            "Курьерская служба / доставка",
        ),
        (
            ("постав", "товар", "продукт", "сыр", "ингредиент"),
            "operating_outflow",
            "Оплата поставщикам",
        ),
        (
            ("эквайр", "комисс", "рко", "расчетно-кассов"),
            "operating_outflow",
            "Эквайринг / банковские комиссии",
        ),
        (
            ("реклам", "маркет", "контекст", "таргет", "seo", "сайт"),
            "operating_outflow",
            "Маркетинг",
        ),
        (
            ("электро", "коммун", "водоснаб", "тепл", "связь", "интернет"),
            "operating_outflow",
            "Коммунальные / связь",
        ),
        (
            ("перевод собствен", "между счет", "внутрен"),
            "technical",
            "Внутренние переводы",
        ),
    ]
    for needles, group, article in rules:
        if any(needle in text for needle in needles):
            return {
                "article_group": group,
                "article": article,
                "review_status": "needs_owner_review",
            }
    if dir_value == "DEBIT":
        return {
            "article_group": "unclassified_outflow",
            "article": "Списания, требуется разметка",
            "review_status": "needs_owner_review",
        }
    return {
        "article_group": "unclassified",
        "article": "Не классифицировано",
        "review_status": "needs_owner_review",
    }


def aggregate_articles(
    transactions: list[dict[str, Any]],
    counterparty_ids: dict[tuple[str, str, str], str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    private_rows: list[dict[str, Any]] = []
    for row in transactions:
        cp_id = counterparty_ids[row["_counterparty_key"]]
        classified = classify_transaction(row)
        row["_article_group"] = classified["article_group"]
        row["_article"] = classified["article"]
        row["_review_status"] = classified["review_status"]
        key = (
            month_of(str(row.get("_operation_date") or row.get("_statement_date"))),
            direction(row),
            classified["article_group"],
            classified["article"],
        )
        groups[key].append(row)
        fields = row.get("_counterparty_fields") or {}
        private_rows.append(
            {
                "date": row.get("_operation_date") or row.get("_statement_date"),
                "direction": direction(row),
                "amount_rub": fmt_money(amount_rub(row)),
                "operation_code": row.get("operationCode") or "",
                "counterparty_id": cp_id,
                "counterparty_name": fields.get("name", ""),
                "counterparty_inn": re.sub(r"\D", "", fields.get("inn", "")),
                "counterparty_account_mask": account_mask(fields.get("account", "")),
                "payment_purpose": row.get("paymentPurpose") or "",
                "article_group": classified["article_group"],
                "article": classified["article"],
                "review_status": classified["review_status"],
            }
        )

    article_rows = []
    for (period, dir_value, group, article), items in sorted(groups.items()):
        amount_total = sum((amount_rub(row) for row in items), Decimal("0"))
        dates = sorted(str(row.get("_operation_date")) for row in items)
        counterparties = sorted({counterparty_ids[row["_counterparty_key"]] for row in items})
        article_rows.append(
            {
                "period": period,
                "direction": dir_value,
                "article_group": group,
                "article": article,
                "transaction_count": len(items),
                "amount_rub_total_abs": fmt_money(amount_total),
                "first_date": dates[0],
                "last_date": dates[-1],
                "counterparty_ids": "; ".join(counterparties),
                "review_status": "needs_owner_review",
            }
        )
    return article_rows, private_rows


def write_outputs(
    daily_rows: list[dict[str, Any]],
    monthly_rows: list[dict[str, Any]],
    operation_code_rows: list[dict[str, Any]],
    counterparty_rows: list[dict[str, Any]],
    private_rows: list[dict[str, Any]],
    article_rows: list[dict[str, Any]],
    private_transaction_rows: list[dict[str, Any]],
) -> None:
    write_csv(
        PROCESSED_DIR / "bank_cashflow_daily.csv",
        daily_rows,
        [
            "date",
            "opening_balance",
            "inflows_total",
            "outflows_total_abs",
            "net_cashflow",
            "closing_balance",
            "summary_credit_turnover",
            "summary_debit_turnover_abs",
            "transaction_credit_count",
            "transaction_debit_count",
            "summary_credit_count",
            "summary_debit_count",
            "transactions_total",
            "balance_formula_delta",
            "credit_turnover_delta",
            "debit_turnover_delta",
            "transaction_count_delta",
            "status",
            "source_files",
        ],
    )
    write_csv(
        PROCESSED_DIR / "bank_cashflow_monthly.csv",
        monthly_rows,
        [
            "period",
            "days_count",
            "transactions_count",
            "opening_balance_first_day",
            "inflows_total",
            "outflows_total_abs",
            "net_cashflow",
            "closing_balance_last_day",
            "min_closing_balance",
            "min_closing_balance_date",
            "max_outflow_day_abs",
            "max_outflow_day",
            "status",
            "source",
        ],
    )
    write_csv(
        PROCESSED_DIR / "bank_operation_codes.csv",
        operation_code_rows,
        [
            "period",
            "direction",
            "operation_code",
            "transaction_count",
            "amount_rub_total_abs",
            "first_date",
            "last_date",
        ],
    )
    write_csv(
        PROCESSED_DIR / "bank_counterparty_summary.csv",
        counterparty_rows,
        [
            "counterparty_id",
            "direction",
            "inn_kind",
            "inn_mask",
            "transaction_count",
            "amount_rub_total_abs",
            "first_date",
            "last_date",
            "operation_codes",
            "mapping_status",
        ],
    )
    write_csv(
        PROCESSED_DIR / "bank_cashflow_articles_draft.csv",
        article_rows,
        [
            "period",
            "direction",
            "article_group",
            "article",
            "transaction_count",
            "amount_rub_total_abs",
            "first_date",
            "last_date",
            "counterparty_ids",
            "review_status",
        ],
    )
    write_csv(
        PRIVATE_DIR / "counterparty_map_private.csv",
        private_rows,
        [
            "counterparty_id",
            "name",
            "inn",
            "account_mask",
            "account_number",
            "bank_bic",
            "comment",
        ],
    )
    write_csv(
        PRIVATE_DIR / "transactions_private.csv",
        private_transaction_rows,
        [
            "date",
            "direction",
            "amount_rub",
            "operation_code",
            "counterparty_id",
            "counterparty_name",
            "counterparty_inn",
            "counterparty_account_mask",
            "payment_purpose",
            "article_group",
            "article",
            "review_status",
        ],
    )


def write_report(
    daily_rows: list[dict[str, Any]],
    monthly_rows: list[dict[str, Any]],
    operation_code_rows: list[dict[str, Any]],
    counterparty_rows: list[dict[str, Any]],
    article_rows: list[dict[str, Any]],
) -> None:
    period = ""
    if daily_rows:
        dates = [str(row["date"]) for row in daily_rows]
        period = f"{min(dates)} - {max(dates)}"
    total_tx = sum(int(row.get("transactions_total") or 0) for row in daily_rows)
    inflows = sum((money(row.get("inflows_total")) for row in daily_rows), Decimal("0"))
    outflows = sum((money(row.get("outflows_total_abs")) for row in daily_rows), Decimal("0"))
    ok_days = sum(1 for row in daily_rows if row.get("status") == "ok")
    top_codes = sorted(
        operation_code_rows,
        key=lambda row: money(row.get("amount_rub_total_abs")),
        reverse=True,
    )[:8]
    top_counterparties = sorted(
        counterparty_rows,
        key=lambda row: money(row.get("amount_rub_total_abs")),
        reverse=True,
    )[:8]
    top_articles = sorted(
        article_rows,
        key=lambda row: money(row.get("amount_rub_total_abs")),
        reverse=True,
    )

    lines = [
        "# Sber API: банковская выписка и ДДС",
        "",
        f"Дата сборки: {dt.date.today().isoformat()}.",
        "",
        "Источник: Sber API `statement/summary` и `statement/transactions`, raw только в `research/private/sber/statement/`.",
        "В открытые processed-файлы не перенесены названия контрагентов, полные счета и назначения платежей.",
        "",
        "## Короткий ответ",
        "",
        f"- Период: `{period}`.",
        f"- Дней с выпиской: {len(daily_rows)}, из них без расхождений summary/transactions: {ok_days}.",
        f"- Операций: {total_tx}.",
        f"- Поступления: {fmt_money(inflows)} руб.",
        f"- Списания: {fmt_money(outflows)} руб.",
        f"- Net cashflow: {fmt_money(inflows - outflows)} руб.",
        "",
        "## Выгруженные файлы",
        "",
        "- `research/processed/sber/bank_cashflow_daily.csv`",
        "- `research/processed/sber/bank_cashflow_monthly.csv`",
        "- `research/processed/sber/bank_operation_codes.csv`",
        "- `research/processed/sber/bank_counterparty_summary.csv`",
        "- `research/processed/sber/bank_cashflow_articles_draft.csv`",
        "- `research/private/sber/processed/counterparty_map_private.csv`",
        "- `research/private/sber/processed/transactions_private.csv`",
        "",
    ]
    if monthly_rows:
        lines.extend(
            [
                "## Месячный агрегат",
                "",
                "| Период | Операций | Поступления | Списания | Net | Остаток на конец |",
                "| --- | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for row in monthly_rows:
            lines.append(
                f"| {row['period']} | {row['transactions_count']} | {row['inflows_total']} | "
                f"{row['outflows_total_abs']} | {row['net_cashflow']} | {row['closing_balance_last_day']} |"
            )
        lines.append("")
    if top_codes:
        lines.extend(
            [
                "## Крупнейшие коды операций",
                "",
                "| Период | Направление | Код | Операций | Сумма |",
                "| --- | --- | --- | ---: | ---: |",
            ]
        )
        for row in top_codes:
            lines.append(
                f"| {row['period']} | {row['direction']} | {row['operation_code']} | "
                f"{row['transaction_count']} | {row['amount_rub_total_abs']} |"
            )
        lines.append("")
    if top_articles:
        lines.extend(
            [
                "## Черновая классификация ДДС",
                "",
                "| Группа | Статья | Направление | Операций | Сумма | Статус |",
                "| --- | --- | --- | ---: | ---: | --- |",
            ]
        )
        for row in top_articles:
            lines.append(
                f"| {row['article_group']} | {row['article']} | {row['direction']} | "
                f"{row['transaction_count']} | {row['amount_rub_total_abs']} | {row['review_status']} |"
            )
        lines.append("")
    if top_counterparties:
        lines.extend(
            [
                "## Крупнейшие псевдонимы контрагентов",
                "",
                "| ID | Направление | ИНН | Операций | Сумма | Статус |",
                "| --- | --- | --- | ---: | ---: | --- |",
            ]
        )
        for row in top_counterparties:
            lines.append(
                f"| {row['counterparty_id']} | {row['direction']} | {row['inn_mask']} | "
                f"{row['transaction_count']} | {row['amount_rub_total_abs']} | {row['mapping_status']} |"
            )
        lines.append("")
    lines.extend(
        [
            "## Следующие действия",
            "",
            "1. Разметить `counterparty_id` по статьям ДДС/P&L в приватной карте.",
            "2. Отделить операционные расходы от кредитов, налогов, внутренних переводов и прочих ниже EBITDA.",
            "3. Сверить банковские поступления с iiko-выручкой с учетом эквайринга, агрегаторов и кассовых лагов.",
            "",
        ]
    )
    write_text(PROCESSED_DIR / "bank_cashflow_report.md", "\n".join(lines))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build sanitized Sber cash-flow aggregates")
    parser.add_argument("--raw-dir", default=str(RAW_DIR))
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    build(parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

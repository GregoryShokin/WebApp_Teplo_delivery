#!/usr/bin/env python3
"""Compare sanitized Sber inflows with iiko revenue focus periods."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BANK_DAILY = PROJECT_ROOT / "research/processed/sber/bank_cashflow_daily.csv"
DEFAULT_IIKO_MONTHLY = PROJECT_ROOT / "research/processed/economic_block/iiko_monthly_gross_margin.csv"
OUTPUT_DIR = PROJECT_ROOT / "research/processed/sber"


def parse_date(value: str) -> dt.date:
    return dt.datetime.strptime(value, "%Y-%m-%d").date()


def money(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def fmt_money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def fmt_pct(value: Decimal) -> str:
    return str((value * Decimal("100")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


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


def build(args: argparse.Namespace) -> None:
    bank_rows = read_csv(Path(args.bank_daily))
    iiko_rows = read_csv(Path(args.iiko_monthly))
    bank_by_date = {parse_date(row["date"]): row for row in bank_rows if row.get("status") == "ok"}

    rows: list[dict[str, Any]] = []
    for source in iiko_rows:
        if str(source.get("is_focus_period", "")).casefold() != "true":
            continue
        start = parse_date(source["period_start"])
        end = parse_date(source["period_end"])
        period_days = [start + dt.timedelta(days=offset) for offset in range((end - start).days + 1)]
        available_days = [day for day in period_days if day in bank_by_date]
        if not available_days:
            continue
        bank_inflows = sum((money(bank_by_date[day]["inflows_total"]) for day in available_days), Decimal("0"))
        bank_outflows = sum((money(bank_by_date[day]["outflows_total_abs"]) for day in available_days), Decimal("0"))
        bank_net = sum((money(bank_by_date[day]["net_cashflow"]) for day in available_days), Decimal("0"))
        iiko_revenue = money(source.get("revenue"))
        diff = bank_inflows - iiko_revenue
        ratio = bank_inflows / iiko_revenue if iiko_revenue else Decimal("0")
        status = "same_period"
        if len(available_days) != len(period_days):
            status = "partial_bank_period"
        rows.append(
            {
                "period": source["period"],
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
                "period_days": len(period_days),
                "bank_days_available": len(available_days),
                "iiko_revenue": fmt_money(iiko_revenue),
                "bank_inflows": fmt_money(bank_inflows),
                "bank_inflows_minus_iiko_revenue": fmt_money(diff),
                "bank_inflows_pct_of_iiko_revenue": fmt_pct(ratio),
                "bank_outflows_abs": fmt_money(bank_outflows),
                "bank_net_cashflow": fmt_money(bank_net),
                "status": status,
                "comment": "Bank inflows are cash receipts, not accrual revenue; compare with lags/acquiring/aggregators in mind.",
            }
        )

    output_csv = OUTPUT_DIR / "bank_iiko_revenue_reconciliation.csv"
    write_csv(
        output_csv,
        rows,
        [
            "period",
            "period_start",
            "period_end",
            "period_days",
            "bank_days_available",
            "iiko_revenue",
            "bank_inflows",
            "bank_inflows_minus_iiko_revenue",
            "bank_inflows_pct_of_iiko_revenue",
            "bank_outflows_abs",
            "bank_net_cashflow",
            "status",
            "comment",
        ],
    )
    write_report(OUTPUT_DIR / "bank_iiko_revenue_reconciliation.md", rows)
    print(f"rows={len(rows)}")
    print(f"output={output_csv.relative_to(PROJECT_ROOT)}")


def write_report(path: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        "# Sber API vs iiko: сверка поступлений и выручки",
        "",
        f"Дата сборки: {dt.date.today().isoformat()}.",
        "",
        "Сравнение диагностическое: банковские поступления являются cash-flow, а iiko-выручка - операционным фактом продаж. Разница может включать эквайринговые лаги, агрегаторов, комиссии, наличные, возвраты и невыручечные поступления.",
        "",
        "| Период | Дней банка | iiko revenue | Bank inflows | Разница | Bank / iiko | Статус |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['period']} | {row['bank_days_available']}/{row['period_days']} | "
            f"{row['iiko_revenue']} | {row['bank_inflows']} | "
            f"{row['bank_inflows_minus_iiko_revenue']} | "
            f"{row['bank_inflows_pct_of_iiko_revenue']}% | {row['status']} |"
        )
    lines.extend(
        [
            "",
            "Следующий шаг: разложить `bank_inflows` по источникам поступлений и сверить с iiko по типам оплаты, а не только общим оборотом.",
            "",
        ]
    )
    write_text(path, "\n".join(lines))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reconcile Sber cash inflows with iiko revenue")
    parser.add_argument("--bank-daily", default=str(DEFAULT_BANK_DAILY))
    parser.add_argument("--iiko-monthly", default=str(DEFAULT_IIKO_MONTHLY))
    return parser.parse_args()


def main() -> int:
    build(parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

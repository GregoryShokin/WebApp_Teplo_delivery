from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models import (
    DepositTransaction,
    Employee,
    PayrollAdjustment,
    PayrollLine,
    PayrollPeriod,
    PayrollRun,
    ShiftLedgerEntry,
)
from app.services.payroll_runner import PayrollNotFoundError

RUN_STATUSES = ("completed", "finalized", "final")
AUDIT_PENALTY_CATEGORY_CODES = {
    "inventory_shortage",
    "inventory_audit_penalty",
    "audit_deferred",
    "audit_penalty",
}
MONEY = Decimal("0.01")


async def build_personal_report(
    session: AsyncSession,
    employee_id: uuid.UUID,
    date_from: date,
    date_to: date,
) -> dict[str, Any]:
    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise PayrollNotFoundError("Employee not found")

    line_rows_result = await session.execute(
        select(PayrollLine, PayrollRun, PayrollPeriod)
        .join(PayrollRun, PayrollLine.run_id == PayrollRun.id)
        .join(PayrollPeriod, PayrollRun.period_id == PayrollPeriod.id)
        .where(
            PayrollLine.employee_id == employee_id,
            PayrollPeriod.start_date <= date_to,
            PayrollPeriod.end_date >= date_from,
            PayrollRun.status.in_(RUN_STATUSES),
        )
        .order_by(PayrollPeriod.start_date.desc())
    )
    line_rows = line_rows_result.all()

    opening_rows_result = await session.execute(
        select(PayrollLine, PayrollRun, PayrollPeriod)
        .join(PayrollRun, PayrollLine.run_id == PayrollRun.id)
        .join(PayrollPeriod, PayrollRun.period_id == PayrollPeriod.id)
        .where(
            PayrollLine.employee_id == employee_id,
            PayrollPeriod.end_date < date_from,
            PayrollRun.status.in_(RUN_STATUSES),
        )
    )
    opening_rows = opening_rows_result.all()

    adjustments_result = await session.scalars(
        select(PayrollAdjustment)
        .options(joinedload(PayrollAdjustment.category))
        .where(
            PayrollAdjustment.employee_id == employee_id,
            PayrollAdjustment.work_date >= date_from,
            PayrollAdjustment.work_date <= date_to,
        )
        .order_by(PayrollAdjustment.work_date.desc())
    )
    adjustments = adjustments_result.all()

    period_start = datetime.combine(date_from, time.min, tzinfo=UTC)
    period_end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=UTC)
    deposit_result = await session.scalars(
        select(DepositTransaction)
        .where(
            DepositTransaction.employee_id == employee_id,
            DepositTransaction.created_at >= period_start,
            DepositTransaction.created_at < period_end,
        )
        .order_by(DepositTransaction.created_at.desc())
    )
    deposit_transactions = deposit_result.all()

    ledger_result = await session.scalars(
        select(ShiftLedgerEntry)
        .where(
            ShiftLedgerEntry.employee_id == employee_id,
            ShiftLedgerEntry.work_date >= date_from,
            ShiftLedgerEntry.work_date <= date_to,
        )
        .order_by(ShiftLedgerEntry.work_date, ShiftLedgerEntry.opened_at)
    )
    ledger_entries = ledger_result.all()

    periods = []
    totals = {
        "base_pay": 0.0,
        "premium": 0.0,
        "percent_pay": 0.0,
        "vacation_pay": 0.0,
        "ndfl_withheld": 0.0,
        "fund_accrual": 0.0,
        "deduction": 0.0,
        "deposit_withholding": 0.0,
        "deposit_payout": 0.0,
        "bonus_total": 0.0,
        "penalty_total": 0.0,
        "total_payable": 0.0,
        "audit_penalty_total": "0.00",
    }
    audit_penalty_total = Decimal("0")
    daily_rows: dict[date, dict[str, Any]] = {}
    ledger_by_date = group_ledger_by_date(ledger_entries)

    for line, run, period in line_rows:
        bonus_total, penalty_total = component_adjustment_totals(line.components)
        deposit_withholding = money_float(component_value(line.components, "deposit_withholding"))
        # «Выдача депозита» (запланированная) хранится в компонентах строки и НЕ входит в
        # total_payable (ФОТ-нетто) — отдаём отдельным полем, чтобы отчёт показывал «на руки».
        deposit_payout = money_float(component_value(line.components, "deposit_payout"))
        item = {
            "period_id": period.id,
            "run_id": run.id,
            "run_status": run.status,
            "role": line.role,
            "period_start": period.start_date,
            "period_end": period.end_date,
            "base_pay": money_float(line.base_pay),
            "premium": money_float(line.premium),
            "percent_pay": money_float(line.percent_pay),
            "vacation_pay": money_float(line.vacation_pay),
            "ndfl_withheld": money_float(getattr(line, "ndfl_withheld", 0)),
            "fund_accrual": money_float(line.fund_accrual),
            "deduction": money_float(line.deduction),
            "deposit_withholding": deposit_withholding,
            "deposit_payout": deposit_payout,
            "bonus_total": bonus_total,
            "penalty_total": penalty_total,
            "total_payable": money_float(line.total_payable),
        }
        periods.append(item)
        for key in totals:
            if key in {"bonus_total", "penalty_total", "audit_penalty_total"}:
                continue
            totals[key] += item[key]
        apply_line_days_to_daily_rows(daily_rows, line, period, date_from, date_to, ledger_by_date)

    serialized_adjustments = []
    for adjustment in adjustments:
        amount = money_float(adjustment.amount)
        amount_decimal = money_decimal(adjustment.amount)
        daily_row = daily_report_row(daily_rows, adjustment.work_date)
        serialized_adjustments.append(
            {
                "id": adjustment.id,
                "type": adjustment.type,
                "work_date": adjustment.work_date,
                "category_id": adjustment.category_id,
                "category_name": adjustment_label(adjustment),
                "custom_label": adjustment.custom_label,
                "amount": amount,
                "comment": adjustment.comment,
            }
        )
        if adjustment.type == "bonus":
            totals["bonus_total"] += amount
            daily_row["premium"] += amount_decimal
        if adjustment.type == "penalty":
            if is_audit_penalty(adjustment):
                audit_penalty_total += amount_decimal
                daily_row["audit_penalty"] += amount_decimal
            else:
                totals["penalty_total"] += amount
                daily_row["penalty"] += amount_decimal
        comment = normalized_text(adjustment.comment)
        if comment:
            daily_row["comments"].append(comment)

    for transaction in deposit_transactions:
        transaction_date = transaction.created_at.date()
        if transaction_date < date_from or transaction_date > date_to:
            continue
        daily_row = daily_report_row(daily_rows, transaction_date)
        if transaction.transaction_type == "accrual":
            daily_row["deposit_in"] += money_decimal(transaction.amount)
        if transaction.transaction_type in {"payout", "dismissal_payout"}:
            daily_row["deposit_out"] += money_decimal(transaction.amount)

    totals["audit_penalty_total"] = money_string(audit_penalty_total)
    daily = [
        serialize_daily_row(row)
        for row in sorted(daily_rows.values(), key=lambda item: item["date"])
    ]
    shifts_count = sum(
        1
        for row in daily_rows.values()
        if row["base_pay"] != Decimal("0") or row["percent_pay"] != Decimal("0")
    )
    opening_balance = sum(money_decimal(line.total_payable) for line, _run, _period in opening_rows)
    period_accrued = sum(money_decimal(line.total_payable) for line, _run, _period in line_rows)
    # TODO: после внедрения PayrollPayout — подключить реальный поток выплат.
    paid_before_period = Decimal("0")
    paid_in_period = Decimal("0")
    opening_balance -= paid_before_period
    closing_balance = opening_balance + period_accrued - paid_in_period

    return {
        "employee_id": employee.id,
        "employee_name": employee.full_name,
        "employee_position": employee.position,
        "date_from": date_from,
        "date_to": date_to,
        "periods": periods,
        "daily": daily,
        "opening_balance": money_string(opening_balance),
        "closing_balance": money_string(closing_balance),
        "shifts_count": shifts_count,
        "adjustments": serialized_adjustments,
        "deposit_transactions": [
            {
                "id": transaction.id,
                "transaction_type": transaction.transaction_type,
                "amount": money_float(transaction.amount),
                "created_at": transaction.created_at,
                "run_id": transaction.run_id,
            }
            for transaction in deposit_transactions
        ],
        "totals": totals,
    }


def apply_line_days_to_daily_rows(
    daily_rows: dict[date, dict[str, Any]],
    line: PayrollLine,
    period: PayrollPeriod,
    date_from: date,
    date_to: date,
    ledger_by_date: dict[date, list[ShiftLedgerEntry]],
) -> None:
    component_days = component_value(line.components, "days")
    if isinstance(component_days, list) and component_days:
        for day in component_days:
            if not isinstance(day, dict):
                continue
            work_date = parse_component_date(day.get("date"))
            if work_date is None or work_date < date_from or work_date > date_to:
                continue
            daily_row = daily_report_row(daily_rows, work_date)
            daily_row["base_pay"] += money_decimal(day.get("base_pay"))
            daily_row["percent_pay"] += money_decimal(day.get("percent_pay"))
            daily_row["vacation_pay"] += money_decimal(day.get("vacation_pay"))
            daily_row["ndfl_withheld"] += money_decimal(day.get("ndfl_withheld"))
            daily_row["fund_accrual"] += money_decimal(day.get("fund_accrual"))
        return

    apply_ledger_fallback_to_daily_rows(
        daily_rows,
        line,
        period,
        date_from,
        date_to,
        ledger_by_date,
    )


def apply_ledger_fallback_to_daily_rows(
    daily_rows: dict[date, dict[str, Any]],
    line: PayrollLine,
    period: PayrollPeriod,
    date_from: date,
    date_to: date,
    ledger_by_date: dict[date, list[ShiftLedgerEntry]],
) -> None:
    range_start = max(period.start_date, date_from)
    range_end = min(period.end_date, date_to)
    entries = [
        entry
        for work_date, day_entries in ledger_by_date.items()
        if range_start <= work_date <= range_end
        for entry in day_entries
    ]
    if not entries:
        return

    minutes_by_date: dict[date, int] = defaultdict(int)
    for entry in entries:
        minutes_by_date[entry.work_date] += ledger_minutes(entry)
    total_minutes = sum(minutes_by_date.values())
    equal_weight = Decimal("1") / Decimal(len(minutes_by_date))
    for work_date in sorted(minutes_by_date):
        weight = (
            Decimal(minutes_by_date[work_date]) / Decimal(total_minutes)
            if total_minutes > 0
            else equal_weight
        )
        daily_row = daily_report_row(daily_rows, work_date)
        daily_row["base_pay"] += money_decimal(line.base_pay) * weight
        daily_row["percent_pay"] += money_decimal(line.percent_pay) * weight
        daily_row["vacation_pay"] += money_decimal(line.vacation_pay) * weight
        daily_row["ndfl_withheld"] += money_decimal(getattr(line, "ndfl_withheld", 0)) * weight
        daily_row["fund_accrual"] += money_decimal(line.fund_accrual) * weight


def group_ledger_by_date(entries: list[ShiftLedgerEntry]) -> dict[date, list[ShiftLedgerEntry]]:
    grouped: dict[date, list[ShiftLedgerEntry]] = defaultdict(list)
    for entry in entries:
        grouped[entry.work_date].append(entry)
    return grouped


def ledger_minutes(entry: ShiftLedgerEntry) -> int:
    if entry.closed_at is None:
        return 0
    return max(int((entry.closed_at - entry.opened_at).total_seconds() // 60), 0)


def daily_report_row(rows: dict[date, dict[str, Any]], work_date: date) -> dict[str, Any]:
    return rows.setdefault(
        work_date,
        {
            "date": work_date,
            "base_pay": Decimal("0"),
            "percent_pay": Decimal("0"),
            "premium": Decimal("0"),
            "vacation_pay": Decimal("0"),
            "ndfl_withheld": Decimal("0"),
            "fund_accrual": Decimal("0"),
            "deposit_in": Decimal("0"),
            "deposit_out": Decimal("0"),
            "penalty": Decimal("0"),
            "audit_penalty": Decimal("0"),
            "comments": [],
        },
    )


def serialize_daily_row(row: dict[str, Any]) -> dict[str, Any]:
    comments = [comment for comment in row["comments"] if comment]
    return {
        "date": row["date"],
        "base_pay": money_float(row["base_pay"]),
        "percent_pay": money_float(row["percent_pay"]),
        "premium": money_float(row["premium"]),
        "vacation_pay": money_float(row["vacation_pay"]),
        "ndfl_withheld": money_float(row["ndfl_withheld"]),
        "fund_accrual": money_float(row["fund_accrual"]),
        "deposit_in": money_float(row["deposit_in"]),
        "deposit_out": money_float(row["deposit_out"]),
        "penalty": money_float(row["penalty"]),
        "audit_penalty": money_float(row["audit_penalty"]),
        "comment": "; ".join(comments) if comments else None,
    }


def component_adjustment_totals(components: object) -> tuple[float, float]:
    adjustments = component_value(components, "adjustments")
    if not isinstance(adjustments, dict):
        return 0.0, 0.0
    return (
        sum_adjustment_items(adjustments.get("bonuses")),
        sum_adjustment_items(adjustments.get("penalties")),
    )


def sum_adjustment_items(value: object) -> float:
    if not isinstance(value, list):
        return 0.0
    return sum(money_float(item.get("amount")) for item in value if isinstance(item, dict))


def component_value(components: object, key: str) -> object:
    if not isinstance(components, dict):
        return None
    return components.get(key)


def adjustment_label(adjustment: PayrollAdjustment) -> str:
    if adjustment.custom_label:
        return adjustment.custom_label
    if adjustment.category is not None:
        return adjustment.category.display_name
    return "Корректировка"


def is_audit_penalty(adjustment: PayrollAdjustment) -> bool:
    category = getattr(adjustment, "category", None)
    category_code = normalized_text(getattr(category, "code", None))
    if category_code in AUDIT_PENALTY_CATEGORY_CODES:
        return True
    comment = normalized_text(getattr(adjustment, "comment", None)).lower()
    label = adjustment_label(adjustment).lower()
    return "ревизи" in comment or "ревизи" in label or "audit" in category_code


def parse_component_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def normalized_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def money_decimal(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def money_string(value: object) -> str:
    return str(money_decimal(value).quantize(MONEY))


def money_float(value: object) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

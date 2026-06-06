from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PayrollLine, PayrollPeriod, PayrollRun


async def build_aggregate(
    session: AsyncSession,
    date_from: date,
    date_to: date,
) -> dict[str, Any]:
    """Aggregate all finalized/completed payroll lines whose periods overlap the range."""
    result = await session.execute(
        select(PayrollLine, PayrollRun, PayrollPeriod)
        .join(PayrollRun, PayrollLine.run_id == PayrollRun.id)
        .join(PayrollPeriod, PayrollRun.period_id == PayrollPeriod.id)
        .where(
            PayrollPeriod.start_date <= date_to,
            PayrollPeriod.end_date >= date_from,
            PayrollRun.status.in_(("completed", "finalized")),
        )
    )
    rows = result.all()

    base_pay = Decimal("0")
    premium = Decimal("0")
    percent_pay = Decimal("0")
    vacation_pay = Decimal("0")
    fund_accrual = Decimal("0")
    ndfl_total = Decimal("0")
    deduction = Decimal("0")
    total_payable = Decimal("0")
    bonus_total = Decimal("0")
    penalty_total = Decimal("0")
    deposit_withheld = Decimal("0")
    employee_ids: set[str] = set()
    run_ids: set[str] = set()
    periods: dict[str, dict[str, Any]] = {}

    for line, run, period in rows:
        base_pay += money_decimal(line.base_pay)
        premium += money_decimal(line.premium)
        percent_pay += money_decimal(line.percent_pay)
        vacation_pay += money_decimal(line.vacation_pay)
        fund_accrual += money_decimal(line.fund_accrual)
        ndfl_total += money_decimal(getattr(line, "ndfl_withheld", 0))
        deduction += money_decimal(line.deduction)
        total_payable += money_decimal(line.total_payable)
        employee_ids.add(str(line.employee_id))
        run_ids.add(str(run.id))

        components = line.components if isinstance(line.components, dict) else {}
        line_bonus_total, line_penalty_total = adjustment_totals(components.get("adjustments"))
        bonus_total += line_bonus_total
        penalty_total += line_penalty_total
        deposit_withheld += deposit_withheld_value(components)

        key = str(period.id)
        bucket = periods.setdefault(
            key,
            {
                "period_id": key,
                "period_start": period.start_date,
                "period_end": period.end_date,
                "run_id": str(run.id),
                "run_status": run.status,
                "lines_count": 0,
                "total_payable": Decimal("0"),
                "base_pay": Decimal("0"),
                "premium": Decimal("0"),
                "percent_pay": Decimal("0"),
                "deduction": Decimal("0"),
                "fund_accrual": Decimal("0"),
            },
        )
        bucket["lines_count"] += 1
        bucket["total_payable"] += money_decimal(line.total_payable)
        bucket["base_pay"] += money_decimal(line.base_pay)
        bucket["premium"] += money_decimal(line.premium)
        bucket["percent_pay"] += money_decimal(line.percent_pay)
        bucket["deduction"] += money_decimal(line.deduction)
        bucket["fund_accrual"] += money_decimal(line.fund_accrual)

    gross = base_pay + premium + percent_pay + vacation_pay + bonus_total
    return {
        "date_from": date_from,
        "date_to": date_to,
        "employees_count": len(employee_ids),
        "runs_count": len(run_ids),
        "totals": {
            "base_pay": money_string(base_pay),
            "premium": money_string(premium),
            "percent_pay": money_string(percent_pay),
            "vacation_pay": money_string(vacation_pay),
            "fund_accrual": money_string(fund_accrual),
            "deduction": money_string(deduction),
            "bonus_total": money_string(bonus_total),
            "penalty_total": money_string(penalty_total),
            "deposit_withheld": money_string(deposit_withheld),
            "ndfl_total": money_string(ndfl_total),
            "gross": money_string(gross),
            "total_payable": money_string(total_payable),
        },
        "periods": sorted(
            (
                {
                    **bucket,
                    "total_payable": money_string(bucket["total_payable"]),
                    "base_pay": money_string(bucket["base_pay"]),
                    "premium": money_string(bucket["premium"]),
                    "percent_pay": money_string(bucket["percent_pay"]),
                    "deduction": money_string(bucket["deduction"]),
                    "fund_accrual": money_string(bucket["fund_accrual"]),
                }
                for bucket in periods.values()
            ),
            key=lambda item: item["period_start"],
            reverse=True,
        ),
    }


def adjustment_totals(adjustments: object) -> tuple[Decimal, Decimal]:
    if isinstance(adjustments, dict):
        return (
            sum_adjustment_items(adjustments.get("bonuses")),
            sum_adjustment_items(adjustments.get("penalties")),
        )
    if isinstance(adjustments, list):
        bonus_total = Decimal("0")
        penalty_total = Decimal("0")
        for item in adjustments:
            if not isinstance(item, dict):
                continue
            amount = money_decimal(item.get("amount"))
            if item.get("type") == "bonus":
                bonus_total += amount
            elif item.get("type") == "penalty":
                penalty_total += amount
        return bonus_total, penalty_total
    return Decimal("0"), Decimal("0")


def sum_adjustment_items(value: object) -> Decimal:
    if not isinstance(value, list):
        return Decimal("0")
    return sum(
        (money_decimal(item.get("amount")) for item in value if isinstance(item, dict)),
        Decimal("0"),
    )


def deposit_withheld_value(components: dict[str, Any]) -> Decimal:
    for key in ("deposit_withheld", "deposit_withholding"):
        if key in components:
            return money_decimal(components.get(key))
    deposit = components.get("deposit")
    if isinstance(deposit, dict) and "withheld" in deposit:
        return money_decimal(deposit.get("withheld"))
    return Decimal("0")


def money_decimal(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def money_string(value: Decimal) -> str:
    return f"{value:.2f}"

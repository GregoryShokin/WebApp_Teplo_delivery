from __future__ import annotations

import uuid
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
)
from app.services.payroll_runner import PayrollNotFoundError


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
            PayrollRun.status.in_(("completed", "finalized", "final")),
        )
        .order_by(PayrollPeriod.start_date.desc())
    )
    line_rows = line_rows_result.all()

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

    periods = []
    totals = {
        "base_pay": 0.0,
        "premium": 0.0,
        "percent_pay": 0.0,
        "vacation_pay": 0.0,
        "fund_accrual": 0.0,
        "deduction": 0.0,
        "deposit_withholding": 0.0,
        "bonus_total": 0.0,
        "penalty_total": 0.0,
        "total_payable": 0.0,
    }

    for line, run, period in line_rows:
        bonus_total, penalty_total = component_adjustment_totals(line.components)
        deposit_withholding = money_float(component_value(line.components, "deposit_withholding"))
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
            "fund_accrual": money_float(line.fund_accrual),
            "deduction": money_float(line.deduction),
            "deposit_withholding": deposit_withholding,
            "bonus_total": bonus_total,
            "penalty_total": penalty_total,
            "total_payable": money_float(line.total_payable),
        }
        periods.append(item)
        for key in totals:
            if key in {"bonus_total", "penalty_total"}:
                continue
            totals[key] += item[key]

    serialized_adjustments = []
    for adjustment in adjustments:
        amount = money_float(adjustment.amount)
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
        if adjustment.type == "penalty":
            totals["penalty_total"] += amount

    return {
        "employee_id": employee.id,
        "employee_name": employee.full_name,
        "employee_position": employee.position,
        "date_from": date_from,
        "date_to": date_to,
        "periods": periods,
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


def money_float(value: object) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

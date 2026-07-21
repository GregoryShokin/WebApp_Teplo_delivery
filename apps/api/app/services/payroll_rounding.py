from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from app.models import PayrollLine

PAYROLL_ROUNDING_UNIT = Decimal("5.00")
_CENTS = Decimal("0.01")


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(_CENTS)


def round_payable_down(amount: object) -> Decimal:
    """Округлить положительную выплату вниз до ближайших 5 рублей."""
    value = _money(amount)
    if value <= 0:
        return value
    units = (value / PAYROLL_ROUNDING_UNIT).to_integral_value(rounding=ROUND_FLOOR)
    return (units * PAYROLL_ROUNDING_UNIT).quantize(_CENTS)


def apply_employee_payable_rounding(lines: Iterable[PayrollLine]) -> dict[str, Any]:
    """Округлить итог каждого сотрудника один раз, даже если у него несколько ролей.

    Разницу снимаем с самых крупных строк сотрудника, не уводя ни одну строку ниже нуля.
    В ``components.payroll_rounding`` сохраняем прозрачную расшифровку для интерфейса.
    """
    by_employee: dict[uuid.UUID, list[PayrollLine]] = defaultdict(list)
    for line in lines:
        by_employee[line.employee_id].append(line)

    rounded_employees = 0
    rounding_total = Decimal("0.00")
    for employee_lines in by_employee.values():
        before = _money(sum((_money(line.total_payable) for line in employee_lines), Decimal("0")))
        after = round_payable_down(before)
        remaining = _money(before - after)
        if remaining <= 0:
            continue
        rounded_employees += 1
        rounding_total += remaining

        for line in sorted(employee_lines, key=lambda row: _money(row.total_payable), reverse=True):
            line_before = _money(line.total_payable)
            reduction = min(line_before, remaining)
            if reduction <= 0:
                continue
            line.total_payable = _money(line_before - reduction)
            components = dict(line.components) if isinstance(line.components, dict) else {}
            components["payroll_rounding"] = {
                "unit": f"{PAYROLL_ROUNDING_UNIT:.2f}",
                "amount": f"{reduction:.2f}",
                "employee_before": f"{before:.2f}",
                "employee_after": f"{after:.2f}",
            }
            line.components = components
            remaining = _money(remaining - reduction)
            if remaining <= 0:
                break
        if remaining != Decimal("0.00"):
            raise RuntimeError("Не удалось распределить округление зарплаты по строкам сотрудника")

    return {
        "rounding_unit": float(PAYROLL_ROUNDING_UNIT),
        "rounding_down_total": float(_money(rounding_total)),
        "employees_rounded": rounded_employees,
    }

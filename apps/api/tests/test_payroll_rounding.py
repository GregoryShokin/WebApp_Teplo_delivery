from __future__ import annotations

import uuid
from decimal import Decimal

from app.models import PayrollLine
from app.services.payroll_rounding import (
    apply_employee_payable_rounding,
    round_payable_down,
)


def _line(employee_id: uuid.UUID, role: str, amount: str) -> PayrollLine:
    return PayrollLine(
        run_id=uuid.uuid4(),
        employee_id=employee_id,
        role=role,
        total_payable=Decimal(amount),
        components={"days": []},
    )


def test_round_payable_down_to_five_rubles() -> None:
    assert round_payable_down("21629.78") == Decimal("21625.00")
    assert round_payable_down("624") == Decimal("620.00")
    assert round_payable_down("625") == Decimal("625.00")
    assert round_payable_down("4.99") == Decimal("0.00")


def test_rounding_is_applied_once_to_employee_total_across_roles() -> None:
    employee_id = uuid.uuid4()
    lines = [
        _line(employee_id, "Пиццерист", "101.99"),
        _line(employee_id, "Сушист", "103.99"),
    ]

    summary = apply_employee_payable_rounding(lines)

    assert sum((line.total_payable for line in lines), Decimal("0")) == Decimal("205.00")
    assert lines[0].total_payable == Decimal("101.99")
    assert lines[1].total_payable == Decimal("103.01")
    assert summary == {
        "rounding_unit": 5.0,
        "rounding_down_total": 0.98,
        "employees_rounded": 1,
    }
    rounding = lines[1].components["payroll_rounding"]
    assert rounding == {
        "unit": "5.00",
        "amount": "0.98",
        "employee_before": "205.98",
        "employee_after": "205.00",
    }


def test_rounding_is_independent_for_each_employee_and_never_goes_negative() -> None:
    first_employee = uuid.uuid4()
    second_employee = uuid.uuid4()
    lines = [
        _line(first_employee, "Пиццерист", "2.00"),
        _line(first_employee, "Сушист", "2.99"),
        _line(second_employee, "Курьер", "11.00"),
    ]

    summary = apply_employee_payable_rounding(lines)

    assert [line.total_payable for line in lines] == [
        Decimal("0.00"),
        Decimal("0.00"),
        Decimal("10.00"),
    ]
    assert summary["rounding_down_total"] == 5.99
    assert summary["employees_rounded"] == 2
    assert all(line.total_payable >= 0 for line in lines)


def test_exact_multiple_of_five_is_not_changed() -> None:
    line = _line(uuid.uuid4(), "Пиццерист", "8150.00")

    summary = apply_employee_payable_rounding([line])

    assert line.total_payable == Decimal("8150.00")
    assert "payroll_rounding" not in line.components
    assert summary["rounding_down_total"] == 0.0
    assert summary["employees_rounded"] == 0

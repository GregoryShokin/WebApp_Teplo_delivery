from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from app.models import Employee
from app.services.staff_taxonomy import (
    CANONICAL_POSITIONS,
    COOKING_STATIONS,
    PAYROLL_ROLE_LABELS,
    canonical_position_name,
    categories_for_payroll_role,
    payroll_role_allows_category,
    payroll_roles_for_position,
    position_requires_pin,
)
from app.services.staff_taxonomy import (
    EMPLOYEE_CATEGORIES as TAXONOMY_EMPLOYEE_CATEGORIES,
)

EmployeeStatus = Literal["active", "inactive", "requires_setup"]
PositionGroup = Literal["cook", "cashier", "no_role"] | None

EMPLOYEE_STATUSES = frozenset({"active", "inactive", "requires_setup"})
EMPLOYEE_CATEGORIES = frozenset(TAXONOMY_EMPLOYEE_CATEGORIES)
COOKING_STATIONS = frozenset(COOKING_STATIONS)
COOK_PAYROLL_ROLES = frozenset({"sushi", "pizza", "shawarma", "prep"})
STAFF_PAYROLL_ROLES = frozenset({"administrator"})

COOK_POSITION_ALIASES = ("Повар",)
STAFF_POSITION_ALIASES = ("Кассир",)
TARGET_POSITION_ALIASES = CANONICAL_POSITIONS
PAYROLL_ROLES = frozenset(PAYROLL_ROLE_LABELS)


def compute_status(
    employee: Employee,
    is_iiko_deleted: bool,
    position_group: PositionGroup,
    assignments: Iterable[object] | None = None,
) -> EmployeeStatus:
    if employee.fire_date is not None:
        return "inactive"
    if is_iiko_deleted:
        return "inactive"
    canonical_position = canonical_position_name(employee.position)
    if canonical_position is None or position_group is None:
        return "requires_setup"
    if (
        position_requires_pin(canonical_position)
        and not getattr(employee, "pin_hash", None)
        and not getattr(employee, "pin_assumed_from_iiko", False)
    ):
        return "requires_setup"

    payroll_roles = payroll_roles_for_position(canonical_position)
    if not payroll_roles:
        return "active"

    if assignments is not None:
        active_assignments = [
            assignment
            for assignment in assignments
            if _assignment_matches_position(assignment, canonical_position)
        ]
        if active_assignments and any(
            getattr(assignment, "is_primary", False) for assignment in active_assignments
        ):
            return "active"
        return "requires_setup"

    shortcut_role = _shortcut_role_for_employee(employee)
    if shortcut_role is None:
        return "requires_setup"
    if not payroll_role_allows_category(shortcut_role, employee.category):
        return "requires_setup"
    return "active"


def is_cook_position(position: str | None) -> bool:
    return canonical_position_name(position) == "Повар"


def is_target_position(position: str | None) -> bool:
    return canonical_position_name(position) is not None


def position_group_for_position(position: str | None) -> PositionGroup:
    canonical_position = canonical_position_name(position)
    if canonical_position is None:
        return None
    if canonical_position == "Повар":
        return "cook"
    if canonical_position == "Кассир":
        return "cashier"
    return "no_role"


def _assignment_matches_position(assignment: object, position: str) -> bool:
    category = getattr(assignment, "category", None)
    payroll_role = getattr(assignment, "payroll_role", None)
    return payroll_role in payroll_roles_for_position(
        position
    ) and category in categories_for_payroll_role(payroll_role)


def _shortcut_role_for_employee(employee: Employee) -> str | None:
    canonical_position = canonical_position_name(employee.position)
    if canonical_position == "Кассир":
        return "administrator"
    if canonical_position == "Повар" and employee.default_cooking_station in COOKING_STATIONS:
        return employee.default_cooking_station
    return None

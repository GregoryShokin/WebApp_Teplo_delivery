from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

from app.models import Employee

EmployeeStatus = Literal["active", "inactive", "requires_setup"]
PositionGroup = Literal["cook", "staff"] | None

EMPLOYEE_STATUSES = frozenset({"active", "inactive", "requires_setup"})
EMPLOYEE_CATEGORIES = frozenset({"category_1", "category_2", "category_3", "intern", "freelancer"})
COOKING_STATIONS = frozenset({"sushi", "pizza", "shawarma"})
COOK_PAYROLL_ROLES = frozenset({"sushi", "pizza", "shawarma", "prep"})
STAFF_PAYROLL_ROLES = frozenset({"administrator", "manager"})

COOK_POSITION_ALIASES = (
    "Повар",
    "Повара",
    "Сушист",
    "Сушисты",
    "Пиццист",
    "Пиццисты",
    "Пиццерист",
    "Пиццеристы",
    "Шаурмист",
    "Шаурмисты",
    "Заготовщик",
    "Заготовщики",
    "Шеф-повар",
    "Шеф повар",
    "Шеф-повара",
)
STAFF_POSITION_ALIASES = (
    "Администратор",
    "Администраторы",
    "Старший администратор",
    "Старшие администраторы",
    "Кассир",
    "Кассиры",
    "Управляющий",
    "Управляющие",
)
TARGET_POSITION_ALIASES = COOK_POSITION_ALIASES + STAFF_POSITION_ALIASES


def _normalize_position(position: str | None) -> str:
    text = (position or "").replace("\xa0", " ").strip()
    text = text.replace("ё", "е").replace("Ё", "Е").casefold()
    text = re.sub(r"\s*[-–—]\s*", "-", text)
    return re.sub(r"\s+", " ", text).strip()


COOK_POSITIONS = frozenset(_normalize_position(value) for value in COOK_POSITION_ALIASES)
STAFF_POSITIONS = frozenset(_normalize_position(value) for value in STAFF_POSITION_ALIASES)
TARGET_POSITIONS = frozenset(_normalize_position(value) for value in TARGET_POSITION_ALIASES)


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
    if not employee.position or position_group is None:
        return "requires_setup"

    if assignments is not None:
        if any(
            _assignment_matches_position_group(assignment, position_group)
            for assignment in assignments
        ):
            return "active"
        return "requires_setup"

    if not employee.category:
        return "requires_setup"
    if position_group == "cook" and not employee.default_cooking_station:
        return "requires_setup"
    return "active"


def is_cook_position(position: str | None) -> bool:
    return _normalize_position(position) in COOK_POSITIONS


def is_target_position(position: str | None) -> bool:
    return _normalize_position(position) in TARGET_POSITIONS


def position_group_for_position(position: str | None) -> PositionGroup:
    if not position:
        return None
    normalized = _normalize_position(position)
    if normalized in COOK_POSITIONS:
        return "cook"
    if normalized in STAFF_POSITIONS:
        return "staff"
    return None


def _assignment_matches_position_group(assignment: object, position_group: PositionGroup) -> bool:
    category = getattr(assignment, "category", None)
    if category is None:
        return False
    payroll_role = getattr(assignment, "payroll_role", None)
    if position_group == "cook":
        return payroll_role in COOK_PAYROLL_ROLES
    if position_group == "staff":
        return payroll_role in STAFF_PAYROLL_ROLES
    return False

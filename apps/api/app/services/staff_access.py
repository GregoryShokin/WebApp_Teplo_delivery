from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import false, or_

from app.auth.permissions import (
    DEFAULT_ROLE_PERMISSIONS,
    FULL_ACCESS_ROLE_CODES,
    VISIBLE_PERMISSION_CODES,
    expand_permission_codes,
    permission_is_granted,
)
from app.models import Employee
from app.services.staff_taxonomy import AUXILIARY_POSITIONS, canonical_position_name


class StaffArea(StrEnum):
    ADMINISTRATION = "administration"
    COOKS = "cooks"
    CASHIERS = "cashiers"
    AUXILIARY = "auxiliary"
    COURIERS = "couriers"


class StaffAction(StrEnum):
    READ = "read"
    CREATE = "create"
    EDIT = "edit"
    DISMISS = "dismiss"
    REINSTATE = "reinstate"
    HISTORY_READ = "history.read"
    ASSIGN_ROLES_CATEGORIES = "assign_roles_categories"


SPECIFIC_STAFF_AREAS: tuple[StaffArea, ...] = (
    StaffArea.ADMINISTRATION,
    StaffArea.COOKS,
    StaffArea.CASHIERS,
    StaffArea.AUXILIARY,
    StaffArea.COURIERS,
)

AREA_PERMISSION_SEGMENTS = {
    StaffArea.ADMINISTRATION: "administration",
    StaffArea.COOKS: "cooks",
    StaffArea.CASHIERS: "cashiers",
    StaffArea.AUXILIARY: "auxiliary",
    StaffArea.COURIERS: "couriers",
}

POSITIONS_BY_AREA = {
    StaffArea.ADMINISTRATION: frozenset(
        {"Управляющий", "Менеджер", "Системный администратор"}
    ),
    StaffArea.COOKS: frozenset({"Повар"}),
    StaffArea.CASHIERS: frozenset({"Кассир"}),
    StaffArea.AUXILIARY: frozenset(AUXILIARY_POSITIONS),
    StaffArea.COURIERS: frozenset({"Курьер"}),
}

LEGACY_FINANCE_MANAGER_STAFF_PERMISSIONS = frozenset(
    code
    for code in VISIBLE_PERMISSION_CODES
    if code.startswith("staff.") and not code.endswith(".reinstate")
)


def effective_staff_permission_codes(actor: Any | None) -> frozenset[str]:
    if actor is None:
        return frozenset()

    roles = frozenset(getattr(actor, "roles", frozenset()) or frozenset())
    permission_codes = set(getattr(actor, "permissions", frozenset()) or frozenset())
    permissions_loaded = bool(getattr(actor, "permissions_loaded", False))

    if roles & FULL_ACCESS_ROLE_CODES:
        permission_codes.update(VISIBLE_PERMISSION_CODES)

    if not permissions_loaded:
        for role in roles:
            permission_codes.update(DEFAULT_ROLE_PERMISSIONS.get(role, frozenset()))

    # Direct route tests and legacy local users still pass finance_manager as a role.
    # Keep its old staff write/read behavior, but leave reinstatement owner-only there.
    if "finance_manager" in roles and not permissions_loaded:
        permission_codes.update(LEGACY_FINANCE_MANAGER_STAFF_PERMISSIONS)

    return expand_permission_codes(permission_codes)


def accessible_staff_areas(actor: Any | None, action: StaffAction) -> frozenset[StaffArea]:
    permission_codes = effective_staff_permission_codes(actor)
    return frozenset(
        area
        for area in SPECIFIC_STAFF_AREAS
        if _staff_area_permission_is_granted(permission_codes, area, action)
    )


def can_access_staff_area(actor: Any | None, area: StaffArea, action: StaffAction) -> bool:
    permission_codes = effective_staff_permission_codes(actor)
    return _staff_area_permission_is_granted(permission_codes, area, action)


def can_access_position(actor: Any | None, position: str | None, action: StaffAction) -> bool:
    return can_access_staff_area(actor, staff_area_for_position(position), action)


def can_access_employee(actor: Any | None, employee: Employee, action: StaffAction) -> bool:
    return can_access_position(actor, employee.position, action)


def ensure_any_staff_access(actor: Any | None, action: StaffAction) -> None:
    if accessible_staff_areas(actor, action):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Недостаточно прав для работы со штатом",
    )


def ensure_staff_area_access(
    actor: Any | None,
    area: StaffArea,
    action: StaffAction,
) -> None:
    if can_access_staff_area(actor, area, action):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Недостаточно прав для этой области сотрудников",
    )


def ensure_position_access(
    actor: Any | None,
    position: str | None,
    action: StaffAction,
) -> None:
    ensure_staff_area_access(actor, staff_area_for_position(position), action)


def ensure_employee_access(
    actor: Any | None,
    employee: Employee,
    action: StaffAction,
) -> None:
    ensure_position_access(actor, employee.position, action)


def staff_area_for_position(position: str | None) -> StaffArea:
    canonical = canonical_position_name(position)
    if canonical == "Повар":
        return StaffArea.COOKS
    if canonical == "Кассир":
        return StaffArea.CASHIERS
    if canonical in AUXILIARY_POSITIONS:
        return StaffArea.AUXILIARY
    if canonical == "Курьер":
        return StaffArea.COURIERS
    return StaffArea.ADMINISTRATION


def employee_access_filter(actor: Any | None, action: StaffAction):
    areas = accessible_staff_areas(actor, action)
    if not areas:
        return false()

    conditions = []
    positions = _positions_for_areas(areas)
    if positions:
        conditions.append(Employee.position.in_(sorted(positions)))
    if StaffArea.ADMINISTRATION in areas:
        conditions.append(Employee.position.is_(None))
    return or_(*conditions) if conditions else false()


def filter_employees_by_staff_access(
    employees: Iterable[Employee],
    actor: Any | None,
    action: StaffAction,
) -> list[Employee]:
    return [
        employee
        for employee in employees
        if can_access_employee(actor, employee, action)
    ]


def employee_ids_with_staff_access(
    employees: Iterable[Employee],
    actor: Any | None,
    action: StaffAction,
) -> frozenset[Any]:
    return frozenset(
        employee.id
        for employee in filter_employees_by_staff_access(employees, actor, action)
    )


def _staff_area_permission_is_granted(
    permission_codes: frozenset[str],
    area: StaffArea,
    action: StaffAction,
) -> bool:
    return permission_is_granted(_area_permission_code(area, action), permission_codes)


def _area_permission_code(area: StaffArea, action: StaffAction) -> str:
    return f"staff.{AREA_PERMISSION_SEGMENTS[area]}.{action.value}"


def _positions_for_areas(areas: Iterable[StaffArea]) -> frozenset[str]:
    positions: set[str] = set()
    for area in areas:
        positions.update(POSITIONS_BY_AREA[area])
    return frozenset(positions)

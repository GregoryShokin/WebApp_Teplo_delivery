from __future__ import annotations

import logging
import re
import uuid
from datetime import date
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Employee, EmployeeRoleAssignment, PayrollRoleCategoryAvailability
from app.services import payroll_config
from app.services.employee_status import (
    COOKING_STATIONS,
    EMPLOYEE_CATEGORIES,
    compute_status,
    position_group_for_position,
)
from app.services.staff_taxonomy import (
    PAYROLL_ROLE_LABELS,
    canonical_position_name,
    categories_for_payroll_role,
    payroll_role_allows_category,
    payroll_roles_for_position,
    position_allows_payroll_role,
    target_position_for_payroll_role,
)

PAYROLL_ROLES = frozenset(PAYROLL_ROLE_LABELS)
COOK_PAYROLL_ROLES = frozenset({"sushi", "pizza", "shawarma", "prep"})
STAFF_PAYROLL_ROLES = frozenset({"administrator"})
logger = logging.getLogger(__name__)


class EmployeeAssignmentError(ValueError):
    pass


class EmployeeAssignmentNotFoundError(LookupError):
    pass


class PrimaryAssignmentRequiredError(EmployeeAssignmentError):
    pass


async def get_assignments(
    session: AsyncSession,
    employee_id: uuid.UUID,
    on_date: date,
) -> list[EmployeeRoleAssignment]:
    result = await session.scalars(
        select(EmployeeRoleAssignment)
        .where(
            EmployeeRoleAssignment.employee_id == employee_id,
            _active_on(on_date),
        )
        .order_by(EmployeeRoleAssignment.is_primary.desc(), EmployeeRoleAssignment.payroll_role)
    )
    return sorted(
        [
            assignment
            for assignment in result.all()
            if assignment.employee_id == employee_id and _assignment_active_on(assignment, on_date)
        ],
        key=lambda assignment: (not assignment.is_primary, assignment.payroll_role),
    )


async def get_assignments_with_pending(
    session: AsyncSession,
    employee_id: uuid.UUID,
    on_date: date,
) -> list[EmployeeRoleAssignment]:
    result = await session.scalars(
        select(EmployeeRoleAssignment)
        .where(
            EmployeeRoleAssignment.employee_id == employee_id,
            or_(
                _active_on(on_date),
                EmployeeRoleAssignment.effective_from > on_date,
            ),
        )
        .order_by(
            EmployeeRoleAssignment.payroll_role,
            EmployeeRoleAssignment.effective_from,
        )
    )
    return [
        assignment
        for assignment in result.all()
        if assignment.employee_id == employee_id
        and (_assignment_active_on(assignment, on_date) or assignment.effective_from > on_date)
    ]


async def set_primary(
    session: AsyncSession,
    employee_id: uuid.UUID,
    role: str,
    category: str,
    *,
    effective_from: date | None = None,
    commit: bool = True,
) -> EmployeeRoleAssignment:
    _validate_role(role)
    _validate_category(category)
    as_of = effective_from or date.today()
    employee = await _get_employee(session, employee_id)
    await _ensure_assignment_valid(session, employee, role, category)
    active_assignments = await get_assignments(session, employee_id, as_of)
    assignment = next(
        (item for item in active_assignments if item.payroll_role == role),
        None,
    )
    if assignment is None:
        next_assignment = await _next_assignment_for_role_after(session, employee_id, role, as_of)
        assignment = EmployeeRoleAssignment(
            employee_id=employee_id,
            payroll_role=role,
            category=category,
            is_primary=False,
            effective_from=as_of,
            effective_to=next_assignment.effective_from if next_assignment is not None else None,
        )
        session.add(assignment)
        await session.flush()
    elif assignment.effective_from == as_of:
        assignment.category = category
    elif assignment.category != category:
        assignment.effective_to = as_of
        next_assignment = await _next_assignment_for_role_after(session, employee_id, role, as_of)
        assignment = EmployeeRoleAssignment(
            employee_id=employee_id,
            payroll_role=role,
            category=category,
            is_primary=assignment.is_primary,
            effective_from=as_of,
            effective_to=next_assignment.effective_from if next_assignment is not None else None,
        )
        session.add(assignment)
        await session.flush()

    await _set_primary_assignment(session, employee_id, assignment, as_of)
    _sync_employee_shortcut(employee, assignment)
    await _refresh_employee_status(session, employee, as_of)
    if commit:
        await _commit_refresh(session, assignment)
    else:
        await session.flush()
    return assignment


async def add_role(
    session: AsyncSession,
    employee_id: uuid.UUID,
    role: str,
    category: str,
    *,
    is_primary: bool = False,
    is_substitute: bool = False,
    effective_from: date | None = None,
    commit: bool = True,
) -> EmployeeRoleAssignment:
    _validate_role(role)
    _validate_category(category)
    as_of = effective_from or date.today()
    employee = await _get_employee(session, employee_id)
    if is_substitute:
        if is_primary:
            raise EmployeeAssignmentError("Подменная роль не может быть основной")
        await _ensure_substitute_assignment_valid(session, employee, role, category)
    else:
        await _ensure_assignment_valid(session, employee, role, category)
    active_assignments = await get_assignments(session, employee_id, as_of)
    if any(assignment.payroll_role == role for assignment in active_assignments):
        raise EmployeeAssignmentError("У сотрудника уже есть активное назначение на эту роль")

    has_primary = any(assignment.is_primary for assignment in active_assignments)
    assignment = EmployeeRoleAssignment(
        employee_id=employee_id,
        payroll_role=role,
        category=category,
        is_primary=False if is_substitute else is_primary or not has_primary,
        is_substitute=is_substitute,
        effective_from=as_of,
    )
    session.add(assignment)
    await session.flush()

    if assignment.is_primary:
        await _set_primary_assignment(session, employee_id, assignment, as_of)
        _sync_employee_shortcut(employee, assignment)
    await _refresh_employee_status(session, employee, as_of)
    if commit:
        await _commit_refresh(session, assignment)
        if assignment.is_substitute:
            await _sync_employee_roles_to_iiko_safely(session, employee_id)
    else:
        await session.flush()
    return assignment


async def add_substitute_role(
    session: AsyncSession,
    employee_id: uuid.UUID,
    payroll_role: str,
    category: str,
    *,
    effective_from: date | None = None,
    actor: Any | None = None,
) -> EmployeeRoleAssignment:
    del actor
    return await add_role(
        session,
        employee_id,
        payroll_role,
        category,
        is_primary=False,
        is_substitute=True,
        effective_from=effective_from,
    )


async def ensure_category_available(
    session: AsyncSession,
    role: str,
    category: str,
) -> None:
    _validate_role(role)
    _validate_category(category)
    await _ensure_category_available(session, role, category)


async def update_assignment(
    session: AsyncSession,
    employee_id: uuid.UUID,
    assignment_id: uuid.UUID,
    *,
    payroll_role: str | None = None,
    category: str | None = None,
    is_primary: bool | None = None,
    is_substitute: bool | None = None,
    effective_from: date | None = None,
) -> EmployeeRoleAssignment:
    assignment = await _get_assignment(session, employee_id, assignment_id)
    as_of = effective_from or date.today()
    employee = await _get_employee(session, employee_id)
    target_role = payroll_role or assignment.payroll_role
    target_category = category or assignment.category
    target_is_substitute = assignment.is_substitute if is_substitute is None else is_substitute
    target_is_primary = assignment.is_primary if is_primary is None else bool(is_primary)

    if target_is_substitute and target_is_primary:
        raise EmployeeAssignmentError("Подменная роль не может быть основной")

    if payroll_role is not None and payroll_role != assignment.payroll_role:
        _validate_role(payroll_role)
        active_assignments = await get_assignments(session, employee_id, as_of)
        if any(
            item.id != assignment.id and item.payroll_role == payroll_role
            for item in active_assignments
        ):
            raise EmployeeAssignmentError("У сотрудника уже есть активное назначение на эту роль")
    if category is not None:
        _validate_category(category)

    if target_is_substitute:
        await _ensure_substitute_assignment_valid(
            session,
            employee,
            target_role,
            target_category,
        )
    else:
        await _ensure_assignment_valid(session, employee, target_role, target_category)
    if is_primary is False and assignment.is_primary:
        raise PrimaryAssignmentRequiredError("Сначала назначьте другую основную роль")

    role_or_category_changed = (
        target_role != assignment.payroll_role or target_category != assignment.category
    )
    substitute_changed = target_is_substitute != assignment.is_substitute
    effective_from_changed = (
        effective_from is not None and effective_from != assignment.effective_from
    )
    if effective_from_changed and assignment.effective_from > date.today():
        await _move_pending_assignment(
            session,
            employee_id,
            assignment,
            as_of,
            payroll_role=target_role,
            category=target_category,
            is_primary=target_is_primary,
            is_substitute=target_is_substitute,
        )
    elif (role_or_category_changed or substitute_changed) and assignment.effective_from < as_of:
        assignment.effective_to = as_of
        next_assignment = await _next_assignment_for_role_after(
            session,
            employee_id,
            target_role,
            as_of,
            exclude_assignment_id=assignment.id,
        )
        assignment = EmployeeRoleAssignment(
            employee_id=employee_id,
            payroll_role=target_role,
            category=target_category,
            is_primary=target_is_primary,
            is_substitute=target_is_substitute,
            effective_from=as_of,
            effective_to=next_assignment.effective_from if next_assignment is not None else None,
        )
        session.add(assignment)
        await session.flush()
    else:
        if payroll_role is not None and payroll_role != assignment.payroll_role:
            assignment.payroll_role = payroll_role
        if category is not None:
            assignment.category = category
        if is_substitute is not None:
            assignment.is_substitute = is_substitute
    if is_primary is True:
        await _set_primary_assignment(session, employee_id, assignment, as_of)

    if assignment.is_primary:
        _sync_employee_shortcut(employee, assignment)
    await _refresh_employee_status(session, employee, as_of)
    await _commit_refresh(session, assignment)
    if assignment.is_substitute or target_is_substitute or substitute_changed:
        await _sync_employee_roles_to_iiko_safely(session, employee_id)
    return assignment


async def cancel_pending_assignment(
    session: AsyncSession,
    employee_id: uuid.UUID,
    assignment_id: uuid.UUID,
) -> None:
    assignment = await _get_assignment(session, employee_id, assignment_id)
    today = date.today()
    if assignment.effective_from <= today:
        raise EmployeeAssignmentError("Только запланированные изменения (будущие) можно удалить")
    was_primary = assignment.is_primary
    was_substitute = assignment.is_substitute

    previous = await session.scalar(
        select(EmployeeRoleAssignment).where(
            EmployeeRoleAssignment.employee_id == employee_id,
            EmployeeRoleAssignment.payroll_role == assignment.payroll_role,
            EmployeeRoleAssignment.effective_to == assignment.effective_from,
        )
    )
    if was_primary and previous is None:
        raise PrimaryAssignmentRequiredError(
            "Эта роль — единственная основная. Сначала назначьте другую основную роль."
        )

    next_pending = await _next_assignment_for_role_after(
        session,
        employee_id,
        assignment.payroll_role,
        assignment.effective_from,
        exclude_assignment_id=assignment.id,
    )

    employee = await _get_employee(session, employee_id)
    await session.delete(assignment)
    await session.flush()

    if previous is not None:
        previous.effective_to = next_pending.effective_from if next_pending else None
        if was_primary:
            _sync_employee_shortcut(employee, previous)

    await _refresh_employee_status(session, employee, today)
    await _commit(session)
    if was_substitute:
        await _sync_employee_roles_to_iiko_safely(session, employee_id)


async def remove_role(
    session: AsyncSession,
    employee_id: uuid.UUID,
    role: str,
    *,
    effective_to: date | None = None,
) -> EmployeeRoleAssignment:
    _validate_role(role)
    as_of = effective_to or date.today()
    assignment = next(
        (
            item
            for item in await get_assignments(session, employee_id, as_of)
            if item.payroll_role == role
        ),
        None,
    )
    if assignment is None:
        raise EmployeeAssignmentNotFoundError("Назначение роли не найдено")
    return await remove_assignment(session, employee_id, assignment.id, effective_to=as_of)


async def remove_assignment(
    session: AsyncSession,
    employee_id: uuid.UUID,
    assignment_id: uuid.UUID,
    *,
    effective_to: date | None = None,
) -> EmployeeRoleAssignment:
    assignment = await _get_assignment(session, employee_id, assignment_id)
    if assignment.is_primary:
        raise PrimaryAssignmentRequiredError("Сначала назначьте другую основную роль")
    was_substitute = assignment.is_substitute

    as_of = effective_to or date.today()
    employee = await _get_employee(session, employee_id)
    assignment.effective_to = as_of
    await _refresh_employee_status(session, employee, as_of)
    await _commit_refresh(session, assignment)
    if was_substitute:
        await _sync_employee_roles_to_iiko_safely(session, employee_id)
    return assignment


async def remove_substitute_role(
    session: AsyncSession,
    employee_id: uuid.UUID,
    assignment_id: uuid.UUID,
    actor: Any | None = None,
) -> EmployeeRoleAssignment:
    del actor
    assignment = await _get_assignment(session, employee_id, assignment_id)
    if not assignment.is_substitute:
        raise EmployeeAssignmentError("Назначение не является подменной ролью")
    return await remove_assignment(session, employee_id, assignment_id)


async def sync_primary_from_shortcut(
    session: AsyncSession,
    employee: Employee,
    *,
    effective_from: date | None = None,
    commit: bool = True,
) -> EmployeeRoleAssignment | None:
    as_of = effective_from or date.today()
    if employee.category is None:
        for assignment in await get_assignments(session, employee.id, as_of):
            assignment.is_primary = False
            assignment.effective_to = as_of
        employee.default_cooking_station = None
        await _refresh_employee_status(session, employee, as_of)
        if commit:
            await _commit(session)
        else:
            await session.flush()
        return None
    role = assignment_role_from_employee(employee)
    if role is None:
        await _refresh_employee_status(session, employee, as_of)
        return None
    await _ensure_assignment_valid(session, employee, role, employee.category)

    active_assignments = await get_assignments(session, employee.id, as_of)
    assignment = next(
        (item for item in active_assignments if item.payroll_role == role),
        None,
    )
    if assignment is None:
        next_assignment = await _next_assignment_for_role_after(session, employee.id, role, as_of)
        assignment = EmployeeRoleAssignment(
            employee_id=employee.id,
            payroll_role=role,
            category=employee.category,
            is_primary=False,
            effective_from=as_of,
            effective_to=next_assignment.effective_from if next_assignment is not None else None,
        )
        session.add(assignment)
        await session.flush()
    elif assignment.effective_from == as_of:
        assignment.category = employee.category
    elif assignment.category != employee.category:
        assignment.effective_to = as_of
        next_assignment = await _next_assignment_for_role_after(session, employee.id, role, as_of)
        assignment = EmployeeRoleAssignment(
            employee_id=employee.id,
            payroll_role=role,
            category=employee.category,
            is_primary=assignment.is_primary,
            effective_from=as_of,
            effective_to=next_assignment.effective_from if next_assignment is not None else None,
        )
        session.add(assignment)
        await session.flush()

    await _set_primary_assignment(session, employee.id, assignment, as_of)
    _sync_employee_shortcut(employee, assignment)
    await _refresh_employee_status(session, employee, as_of)
    if commit:
        await _commit_refresh(session, assignment)
    else:
        await session.flush()
    return assignment


def assignment_role_for_payroll_context(role: str | None, station: str | None) -> str | None:
    station_key = _normalize_ascii(station)
    if station_key in COOKING_STATIONS:
        return station_key
    return assignment_role_from_position(role)


def assignment_role_from_employee(employee: Employee) -> str | None:
    station = _normalize_ascii(employee.default_cooking_station)
    if station in COOKING_STATIONS:
        return station
    if canonical_position_name(employee.position) == "Кассир":
        return "administrator"
    return assignment_role_from_position(employee.position)


def assignment_role_from_position(position: str | None) -> str | None:
    normalized = _normalize_position(position)
    if "суш" in normalized:
        return "sushi"
    if "пиц" in normalized:
        return "pizza"
    if "шаур" in normalized:
        return "shawarma"
    if "заготов" in normalized:
        return "prep"
    if "администратор" in normalized or "кассир" in normalized:
        return "administrator"
    return None


def role_matches_position_group(payroll_role: str | None, position_group: str | None) -> bool:
    if position_group == "cook":
        return payroll_role in COOK_PAYROLL_ROLES
    if position_group == "cashier":
        return payroll_role in STAFF_PAYROLL_ROLES
    return False


async def _set_primary_assignment(
    session: AsyncSession,
    employee_id: uuid.UUID,
    assignment: EmployeeRoleAssignment,
    as_of: date,
) -> None:
    await session.execute(
        update(EmployeeRoleAssignment)
        .where(
            EmployeeRoleAssignment.employee_id == employee_id,
            EmployeeRoleAssignment.id != assignment.id,
            _active_on(as_of),
        )
        .values(is_primary=False)
    )
    assignment.is_primary = True
    await session.flush()


async def _refresh_employee_status(
    session: AsyncSession,
    employee: Employee,
    as_of: date,
) -> None:
    active_assignments = await get_assignments(session, employee.id, as_of)
    employee.status = compute_status(
        employee,
        is_iiko_deleted=employee.status == "inactive",
        position_group=position_group_for_position(employee.position),
        assignments=active_assignments,
    )


async def _get_employee(session: AsyncSession, employee_id: uuid.UUID) -> Employee:
    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise EmployeeAssignmentNotFoundError("Сотрудник не найден")
    return employee


async def _get_assignment(
    session: AsyncSession,
    employee_id: uuid.UUID,
    assignment_id: uuid.UUID,
) -> EmployeeRoleAssignment:
    assignment = await session.scalar(
        select(EmployeeRoleAssignment).where(
            EmployeeRoleAssignment.id == assignment_id,
            EmployeeRoleAssignment.employee_id == employee_id,
        )
    )
    if assignment is None:
        raise EmployeeAssignmentNotFoundError("Назначение роли не найдено")
    return assignment


async def _next_assignment_for_role_after(
    session: AsyncSession,
    employee_id: uuid.UUID,
    payroll_role: str,
    effective_from: date,
    *,
    exclude_assignment_id: uuid.UUID | None = None,
) -> EmployeeRoleAssignment | None:
    query = select(EmployeeRoleAssignment).where(
        EmployeeRoleAssignment.employee_id == employee_id,
        EmployeeRoleAssignment.payroll_role == payroll_role,
        EmployeeRoleAssignment.effective_from > effective_from,
    )
    if exclude_assignment_id is not None:
        query = query.where(EmployeeRoleAssignment.id != exclude_assignment_id)
    return await session.scalar(query.order_by(EmployeeRoleAssignment.effective_from).limit(1))


async def _previous_assignment_for_role_before(
    session: AsyncSession,
    employee_id: uuid.UUID,
    payroll_role: str,
    effective_from: date,
    *,
    exclude_assignment_id: uuid.UUID | None = None,
) -> EmployeeRoleAssignment | None:
    query = select(EmployeeRoleAssignment).where(
        EmployeeRoleAssignment.employee_id == employee_id,
        EmployeeRoleAssignment.payroll_role == payroll_role,
        EmployeeRoleAssignment.effective_from < effective_from,
    )
    if exclude_assignment_id is not None:
        query = query.where(EmployeeRoleAssignment.id != exclude_assignment_id)
    return await session.scalar(
        query.order_by(EmployeeRoleAssignment.effective_from.desc()).limit(1)
    )


async def _move_pending_assignment(
    session: AsyncSession,
    employee_id: uuid.UUID,
    assignment: EmployeeRoleAssignment,
    effective_from: date,
    *,
    payroll_role: str,
    category: str,
    is_primary: bool,
    is_substitute: bool,
) -> None:
    previous = await _previous_assignment_for_role_before(
        session,
        employee_id,
        assignment.payroll_role,
        assignment.effective_from,
        exclude_assignment_id=assignment.id,
    )
    next_for_old_role = await _next_assignment_for_role_after(
        session,
        employee_id,
        assignment.payroll_role,
        assignment.effective_from,
        exclude_assignment_id=assignment.id,
    )
    if previous is not None:
        previous.effective_to = next_for_old_role.effective_from if next_for_old_role else None

    assignment.payroll_role = payroll_role
    assignment.category = category
    assignment.is_primary = is_primary
    assignment.is_substitute = is_substitute
    assignment.effective_from = effective_from

    next_for_new_role = await _next_assignment_for_role_after(
        session,
        employee_id,
        payroll_role,
        effective_from,
        exclude_assignment_id=assignment.id,
    )
    previous_for_new_role = await _previous_assignment_for_role_before(
        session,
        employee_id,
        payroll_role,
        effective_from,
        exclude_assignment_id=assignment.id,
    )
    if previous_for_new_role is not None:
        previous_for_new_role.effective_to = effective_from
    assignment.effective_to = next_for_new_role.effective_from if next_for_new_role else None
    await session.flush()


async def _commit_refresh(session: AsyncSession, assignment: EmployeeRoleAssignment) -> None:
    await _commit(session)
    await session.refresh(assignment)


async def _commit(session: AsyncSession) -> None:
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise EmployeeAssignmentError(
            "Назначение конфликтует с существующей историей ролей"
        ) from exc


def _sync_employee_shortcut(employee: Employee, assignment: EmployeeRoleAssignment) -> None:
    employee.category = assignment.category
    employee.default_cooking_station = (
        assignment.payroll_role if assignment.payroll_role in COOKING_STATIONS else None
    )


def _active_on(on_date: date) -> Any:
    return and_(
        EmployeeRoleAssignment.effective_from <= on_date,
        or_(
            EmployeeRoleAssignment.effective_to.is_(None),
            EmployeeRoleAssignment.effective_to > on_date,
        ),
    )


def _assignment_active_on(assignment: EmployeeRoleAssignment, on_date: date) -> bool:
    return assignment.effective_from <= on_date and (
        assignment.effective_to is None or assignment.effective_to > on_date
    )


def _validate_role(role: str) -> None:
    if role not in PAYROLL_ROLES:
        raise EmployeeAssignmentError("Некорректная роль начисления")


def _validate_category(category: str) -> None:
    if category not in EMPLOYEE_CATEGORIES:
        raise EmployeeAssignmentError("Некорректная категория сотрудника")


async def _ensure_category_available(
    session: AsyncSession,
    payroll_role: str,
    category: str,
) -> None:
    if not payroll_role_allows_category(payroll_role, category):
        raise EmployeeAssignmentError("Категория недоступна для этой роли")

    position_group = PAYROLL_ROLE_LABELS.get(payroll_role)
    if position_group is None:
        return

    records = list(
        (
            await session.scalars(
                select(PayrollRoleCategoryAvailability).where(
                    PayrollRoleCategoryAvailability.position_group == position_group
                )
            )
        ).all()
    )
    if not records:
        return
    if any(record.category == category and record.is_enabled for record in records):
        return
    message = "Категория недоступна для этой роли"
    raise EmployeeAssignmentError(message)


async def _ensure_substitute_assignment_valid(
    session: AsyncSession,
    employee: Employee,
    payroll_role: str,
    category: str,
) -> None:
    canonical_position = canonical_position_name(employee.position)
    if canonical_position is None:
        raise EmployeeAssignmentError("У сотрудника не задана каноническая должность")
    if category not in categories_for_payroll_role(payroll_role):
        raise EmployeeAssignmentError("Категория недоступна для этой роли")
    await _ensure_category_available(session, payroll_role, category)

    target_position = target_position_for_payroll_role(payroll_role)
    pairs = await payroll_config.get_substitute_pairs(session)
    if payroll_config.is_substitute_pair_allowed(
        pairs,
        canonical_position,
        target_position,
    ):
        return
    raise EmployeeAssignmentError(
        f"Подмена {canonical_position} → {target_position} не разрешена. "
        "Настройте в Настройках"
    )


async def _ensure_assignment_valid(
    session: AsyncSession,
    employee: Employee,
    payroll_role: str,
    category: str,
) -> None:
    canonical_position = canonical_position_name(employee.position)
    if canonical_position is None:
        raise EmployeeAssignmentError("У сотрудника не задана каноническая должность")
    if not position_allows_payroll_role(canonical_position, payroll_role):
        raise EmployeeAssignmentError("Роль не соответствует должности сотрудника")
    if payroll_role not in payroll_roles_for_position(canonical_position):
        raise EmployeeAssignmentError("Роль не соответствует должности сотрудника")
    if category not in categories_for_payroll_role(payroll_role):
        raise EmployeeAssignmentError("Категория недоступна для этой роли")
    await _ensure_category_available(session, payroll_role, category)


async def _sync_employee_roles_to_iiko_safely(
    session: AsyncSession,
    employee_id: uuid.UUID,
) -> None:
    try:
        from app.services.iiko_sync import sync_employee_roles_to_iiko

        await sync_employee_roles_to_iiko(session, employee_id)
    except Exception:  # noqa: BLE001 - iiko sync must not roll back local assignment edits
        logger.exception(
            "Failed to sync substitute roles to iiko",
            extra={"employee_id": employee_id},
        )


def _normalize_position(value: str | None) -> str:
    text = (value or "").replace("\xa0", " ").strip()
    text = text.replace("ё", "е").replace("Ё", "Е").casefold()
    text = re.sub(r"\s*[-–—]\s*", "-", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_ascii(value: str | None) -> str:
    return (value or "").strip().casefold()

from __future__ import annotations

import http.client as _http_client
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentActor, get_current_actor, require_finance_manager_plus
from app.core.security import hash_password
from app.db.session import get_session
from app.models import AgentAction, AgentRun, Employee, EmployeeRoleAssignment, ShiftLedgerEntry
from app.schemas.employees import (
    EmployeeCreateRequest,
    EmployeeDismissRequest,
    EmployeePatch,
    EmployeePinChangeRequest,
    EmployeeRead,
    EmployeeRoleAssignmentCreate,
    EmployeeRoleAssignmentPatch,
    EmployeeRoleAssignmentRead,
    IikoEmployeeRoleRead,
    SyncResultRead,
)
from app.services import employee_assignments as employee_assignment_service
from app.services.employee_status import (
    COOKING_STATIONS,
    EMPLOYEE_CATEGORIES,
    EMPLOYEE_STATUSES,
    compute_status,
    is_cook_position,
    position_group_for_position,
)
from app.services.iiko_sync import (
    IikoEmployeeOperationError,
    IikoEmployeeRole,
    get_iiko_employee_roles,
    sync_employees,
)
from app.services.iiko_sync import (
    create_iiko_employee as create_iiko_employee_in_iiko,
)
from app.services.iiko_sync import (
    dismiss_iiko_employee as dismiss_iiko_employee_in_iiko,
)
from app.services.shift_ledger import ledger_entry_snapshot
from app.services.staff_taxonomy import (
    PAYROLL_ROLE_LABELS,
    canonical_position_name,
    categories_for_payroll_role,
    is_create_position,
    payroll_roles_for_position,
    reset_inapplicable_premiums,
    validate_premiums,
)

router = APIRouter()
OWNER_ONLY = frozenset({"owner"})

READ_ONLY_FIELDS = {"id", "full_name", "iiko_id", "iiko_sync_at", "created_at", "updated_at"}
APP_MANAGED_FIELDS = {
    "position",
    "category",
    "default_cooking_station",
    "is_senior",
    "is_deputy_senior",
    "hire_date",
    "fire_date",
}
COMPUTED_FIELDS = {"status"}
ALLOWED_CREATE_PAYROLL_ROLES = frozenset(PAYROLL_ROLE_LABELS)


@dataclass(frozen=True, slots=True)
class ResolvedCreateRole:
    payroll_role: str
    category: str
    is_primary: bool


@router.get("", response_model=list[EmployeeRead])
@router.get("/", response_model=list[EmployeeRead], include_in_schema=False)
async def list_employees(
    session: Annotated[AsyncSession, Depends(get_session)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    category: str | None = None,
    cooking_station: Annotated[str | None, Query(alias="cooking_station")] = None,
    search: str | None = None,
) -> list[Employee]:
    today = date.today()
    query = select(Employee).options(selectinload(Employee.role_assignments))
    if status_filter:
        if status_filter not in EMPLOYEE_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid employee status")
        query = query.where(Employee.status == status_filter)
    if category:
        if category not in EMPLOYEE_CATEGORIES:
            raise HTTPException(status_code=400, detail="Invalid employee category")
        query = query.where(_active_assignment_exists(today, category=category))
    if cooking_station:
        if cooking_station not in COOKING_STATIONS:
            raise HTTPException(status_code=400, detail="Invalid cooking station")
        query = query.where(_active_assignment_exists(today, payroll_role=cooking_station))
    if search:
        query = query.where(Employee.full_name.ilike(f"%{search}%"))

    result = await session.scalars(query.order_by(Employee.full_name))
    return list(result.all())


@router.post("/sync", response_model=SyncResultRead)
async def trigger_employee_sync(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    mode: Annotated[Literal["incremental", "reset"], Query()] = "incremental",
) -> dict[str, int]:
    require_finance_manager_plus(actor)
    try:
        result = await sync_employees(session, run_reason="manual", mode=mode)
    except _http_client.IncompleteRead as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="iiko не отвечает — сервер оборвал соединение. Попробуйте через минуту.",
        ) from exc
    return result.as_dict()


@router.get("/iiko-roles", response_model=list[IikoEmployeeRoleRead])
async def list_iiko_employee_roles(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[IikoEmployeeRoleRead]:
    require_finance_manager_plus(actor)
    try:
        roles = await get_iiko_employee_roles(session)
    except _http_client.IncompleteRead as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="iiko не отвечает — сервер оборвал соединение. Попробуйте через минуту.",
        ) from exc
    except IikoEmployeeOperationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    return [
        IikoEmployeeRoleRead(
            id=role.id,
            name=role.name,
            code=role.code,
            deleted=role.deleted,
        )
        for role in roles
        if is_create_position(role.name)
    ]


@router.post("", response_model=EmployeeRead, status_code=status.HTTP_201_CREATED)
@router.post(
    "/",
    response_model=EmployeeRead,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
async def create_employee(
    payload: EmployeeCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> Employee:
    require_finance_manager_plus(actor)
    try:
        iiko_role = await _resolve_create_iiko_position(session, payload.iiko_role_id)
        canonical_position = canonical_position_name(iiko_role.name)
        if canonical_position is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Выбранная должность iiko недоступна для создания сотрудника",
            )
        _validate_premium_flags(
            canonical_position,
            is_senior=payload.is_senior,
            is_deputy_senior=payload.is_deputy_senior,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
        resolved_roles = await _resolve_create_roles(
            session,
            payload,
            position=canonical_position,
        )
    except _http_client.IncompleteRead as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="iiko не отвечает — сотрудник не создан. Попробуйте через минуту.",
        ) from exc
    except IikoEmployeeOperationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    try:
        iiko_employee = await create_iiko_employee_in_iiko(
            session,
            full_name=payload.full_name,
            role_id=iiko_role.id,
            pin_code=payload.pin_code,
        )
    except _http_client.IncompleteRead as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="iiko не отвечает — сотрудник не создан. Попробуйте через минуту.",
        ) from exc
    except IikoEmployeeOperationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    now = datetime.now(UTC)
    pin_hash = hash_password(payload.pin_code)
    employee = await session.scalar(
        select(Employee).where(Employee.iiko_id == iiko_employee.iiko_id)
    )
    before = _employee_lifecycle_snapshot(employee) if employee is not None else None
    created = employee is None
    if employee is None:
        employee = Employee(
            full_name=iiko_employee.full_name,
            iiko_id=iiko_employee.iiko_id,
            position=canonical_position,
            category=None,
            default_cooking_station=None,
            is_senior=payload.is_senior,
            is_deputy_senior=payload.is_deputy_senior,
            pin_hash=pin_hash,
            pin_set_at=now,
            iiko_sync_at=now,
        )
        session.add(employee)
    else:
        employee.full_name = iiko_employee.full_name
        employee.position = canonical_position
        employee.is_senior = payload.is_senior
        employee.is_deputy_senior = payload.is_deputy_senior
        employee.pin_hash = pin_hash
        employee.pin_set_at = now
        employee.fire_date = None
        employee.fire_reason = None
        employee.iiko_sync_at = now

    today = date.today()
    await session.flush()
    assignment_snapshots: list[dict[str, Any]] = []
    try:
        for role in sorted(resolved_roles, key=lambda item: not item.is_primary):
            assignment = await employee_assignment_service.add_role(
                session,
                employee.id,
                role.payroll_role,
                role.category,
                is_primary=role.is_primary,
                effective_from=today,
                commit=False,
            )
            assignment_snapshots.append(_employee_assignment_snapshot(assignment))
    except employee_assignment_service.EmployeeAssignmentError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    assignments = await employee_assignment_service.get_assignments(session, employee.id, today)
    employee.status = compute_status(
        employee,
        is_iiko_deleted=False,
        position_group=position_group_for_position(employee.position),
        assignments=assignments,
    )
    employee.updated_at = now
    await session.flush()
    after = {
        **_employee_lifecycle_snapshot(employee),
        "iiko_role_id": iiko_employee.role_id,
        "iiko_role_name": iiko_role.name,
        "iiko_role_code": iiko_employee.role_code,
        "is_target_position": iiko_employee.is_target_position,
        "roles": assignment_snapshots,
    }
    await _add_employee_lifecycle_action(
        session,
        action_type="create" if created else "upsert_from_iiko_create",
        employee=employee,
        before=before,
        after=after,
        now=now,
        actor=actor,
    )

    await session.commit()
    await session.refresh(employee)
    return await _get_employee_or_404(session, employee.id, include_assignments=True)


@router.post("/{employee_id}/dismiss", response_model=EmployeeRead)
async def dismiss_employee(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    payload: Annotated[EmployeeDismissRequest | None, Body()] = None,
) -> Employee:
    require_finance_manager_plus(actor)
    dismiss_payload = payload or EmployeeDismissRequest()
    employee = await _get_employee_or_404(session, employee_id)
    if employee.status == "inactive" or employee.fire_date is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Сотрудник уже уволен")

    before = _employee_lifecycle_snapshot(employee)
    try:
        await dismiss_iiko_employee_in_iiko(session, iiko_id=employee.iiko_id)
    except _http_client.IncompleteRead as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="iiko не отвечает — локальный статус не изменён. Попробуйте через минуту.",
        ) from exc
    except IikoEmployeeOperationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    now = datetime.now(UTC)
    reason = dismiss_payload.reason.strip() if dismiss_payload.reason else None
    employee.status = "inactive"
    employee.fire_date = dismiss_payload.fire_date or date.today()
    employee.fire_reason = reason or None
    employee.updated_at = now
    after = _employee_lifecycle_snapshot(employee)
    agent_run_id = await _add_employee_lifecycle_action(
        session,
        action_type="dismiss",
        employee=employee,
        before=before,
        after=after,
        now=now,
        actor=actor,
        reason=reason,
    )
    await _close_active_shift_entries_on_dismiss(
        session,
        employee,
        now=now,
        agent_run_id=agent_run_id,
    )

    await session.commit()
    await session.refresh(employee)
    if isinstance(session, AsyncSession):
        return await _get_employee_or_404(session, employee_id, include_assignments=True)
    return employee


@router.post("/{employee_id}/reinstate", response_model=EmployeeRead)
async def reinstate_employee(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> Employee:
    _require_owner(actor)
    employee = await _get_employee_or_404(session, employee_id)
    before = _employee_lifecycle_snapshot(employee)
    now = datetime.now(UTC)
    employee.fire_date = None
    employee.fire_reason = None

    assignments = None
    if isinstance(session, AsyncSession):
        assignments = await employee_assignment_service.get_assignments(
            session,
            employee.id,
            date.today(),
        )
    employee.status = compute_status(
        employee,
        is_iiko_deleted=False,
        position_group=position_group_for_position(employee.position),
        assignments=assignments,
    )
    employee.updated_at = now
    after = _employee_lifecycle_snapshot(employee)
    await _add_employee_lifecycle_action(
        session,
        action_type="reinstate",
        employee=employee,
        before=before,
        after=after,
        now=now,
        actor=actor,
    )

    await session.commit()
    await session.refresh(employee)
    if isinstance(session, AsyncSession):
        return await _get_employee_or_404(session, employee_id, include_assignments=True)
    return employee


@router.post("/{employee_id}/pin", response_model=EmployeeRead)
async def change_employee_pin(
    employee_id: uuid.UUID,
    payload: EmployeePinChangeRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> Employee:
    require_finance_manager_plus(actor)
    employee = await _get_employee_or_404(session, employee_id)
    before = _employee_lifecycle_snapshot(employee)
    now = datetime.now(UTC)
    employee.pin_hash = hash_password(payload.pin_code)
    employee.pin_set_at = now

    assignments = None
    if isinstance(session, AsyncSession):
        assignments = await employee_assignment_service.get_assignments(
            session,
            employee.id,
            date.today(),
        )
    employee.status = compute_status(
        employee,
        is_iiko_deleted=employee.status == "inactive",
        position_group=position_group_for_position(employee.position),
        assignments=assignments,
    )
    employee.updated_at = now
    after = {**_employee_lifecycle_snapshot(employee), "pin_changed": True}
    await _add_employee_lifecycle_action(
        session,
        action_type="change_pin",
        employee=employee,
        before=before,
        after=after,
        now=now,
        actor=actor,
    )
    await session.commit()
    await session.refresh(employee)
    if isinstance(session, AsyncSession):
        return await _get_employee_or_404(session, employee_id, include_assignments=True)
    return employee


@router.get("/{employee_id}/assignments", response_model=list[EmployeeRoleAssignmentRead])
async def list_employee_assignments(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    on_date: date | None = None,
) -> list[EmployeeRoleAssignment]:
    await _get_employee_or_404(session, employee_id)
    return await employee_assignment_service.get_assignments(
        session,
        employee_id,
        on_date or date.today(),
    )


@router.post("/{employee_id}/assignments", response_model=EmployeeRoleAssignmentRead)
async def create_employee_assignment(
    employee_id: uuid.UUID,
    payload: EmployeeRoleAssignmentCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> EmployeeRoleAssignment:
    require_finance_manager_plus(actor)
    await _get_employee_or_404(session, employee_id)
    try:
        assignment = await employee_assignment_service.add_role(
            session,
            employee_id,
            payload.payroll_role,
            payload.category,
            is_primary=payload.is_primary,
            effective_from=payload.effective_from,
        )
    except employee_assignment_service.EmployeeAssignmentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except employee_assignment_service.EmployeeAssignmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    now = datetime.now(UTC)
    await _add_manual_action(
        session,
        action_type="add_role_assignment",
        target_table="employee_role_assignment",
        target_id=assignment.id,
        before=None,
        after=_assignment_snapshot(assignment),
        now=now,
        actor=actor,
        employee_id=employee_id,
    )
    await session.commit()
    await session.refresh(assignment)
    return assignment


@router.patch(
    "/{employee_id}/assignments/{assignment_id}",
    response_model=EmployeeRoleAssignmentRead,
)
async def patch_employee_assignment(
    employee_id: uuid.UUID,
    assignment_id: uuid.UUID,
    payload: EmployeeRoleAssignmentPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> EmployeeRoleAssignment:
    require_finance_manager_plus(actor)
    if not payload.model_fields_set:
        raise HTTPException(status_code=400, detail="Assignment patch is empty")
    if "payroll_role" in payload.model_fields_set and payload.payroll_role is None:
        raise HTTPException(status_code=400, detail="payroll_role cannot be null")
    if "category" in payload.model_fields_set and payload.category is None:
        raise HTTPException(status_code=400, detail="category cannot be null")
    assignment_before = await _get_assignment_or_404(session, employee_id, assignment_id)
    before = _assignment_snapshot(assignment_before)
    try:
        assignment = await employee_assignment_service.update_assignment(
            session,
            employee_id,
            assignment_id,
            payroll_role=(
                payload.payroll_role if "payroll_role" in payload.model_fields_set else None
            ),
            category=payload.category if "category" in payload.model_fields_set else None,
            is_primary=payload.is_primary if "is_primary" in payload.model_fields_set else None,
        )
    except employee_assignment_service.EmployeeAssignmentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except employee_assignment_service.EmployeeAssignmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    now = datetime.now(UTC)
    await _add_manual_action(
        session,
        action_type="update_role_assignment",
        target_table="employee_role_assignment",
        target_id=assignment.id,
        before=before,
        after=_assignment_snapshot(assignment),
        now=now,
        actor=actor,
        employee_id=employee_id,
    )
    await session.commit()
    await session.refresh(assignment)
    return assignment


@router.delete(
    "/{employee_id}/assignments/{assignment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_employee_assignment(
    employee_id: uuid.UUID,
    assignment_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> None:
    require_finance_manager_plus(actor)
    assignment_before = await _get_assignment_or_404(session, employee_id, assignment_id)
    before = _assignment_snapshot(assignment_before)
    try:
        assignment = await employee_assignment_service.remove_assignment(
            session, employee_id, assignment_id
        )
    except employee_assignment_service.EmployeeAssignmentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except employee_assignment_service.EmployeeAssignmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    now = datetime.now(UTC)
    await _add_manual_action(
        session,
        action_type="remove_role_assignment",
        target_table="employee_role_assignment",
        target_id=assignment.id,
        before=before,
        after=_assignment_snapshot(assignment),
        now=now,
        actor=actor,
        employee_id=employee_id,
    )
    await session.commit()


@router.get("/{employee_id}", response_model=EmployeeRead)
async def get_employee(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Employee:
    return await _get_employee_or_404(session, employee_id, include_assignments=True)


@router.patch("/{employee_id}", response_model=EmployeeRead)
async def patch_employee(
    employee_id: uuid.UUID,
    payload: Annotated[dict[str, Any], Body()],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> Employee:
    require_finance_manager_plus(actor)
    normalized_payload = _normalize_patch_payload(payload)
    _validate_patch_payload(normalized_payload)

    patch = EmployeePatch.model_validate(normalized_payload)
    employee = await _get_employee_or_404(session, employee_id)
    before = _employee_lifecycle_snapshot(employee)
    is_iiko_deleted = employee.status == "inactive"
    position_changed = "position" in patch.model_fields_set
    target_position = employee.position
    if position_changed:
        if patch.position is None:
            raise HTTPException(status_code=400, detail="Должность обязательна")
        target_position = canonical_position_name(patch.position)
        if target_position is None:
            raise HTTPException(status_code=400, detail="Должность не входит в канонический список")

    _validate_explicit_premium_patch(
        patch,
        target_position,
    )

    for field in patch.model_fields_set:
        if field == "position":
            employee.position = target_position
        else:
            setattr(employee, field, getattr(patch, field))

    if not is_cook_position(employee.position):
        if "default_cooking_station" in patch.model_fields_set and employee.default_cooking_station:
            raise HTTPException(status_code=400, detail="Цех допустим только для поваров")
        employee.default_cooking_station = None
    if not payroll_roles_for_position(employee.position):
        if "category" in patch.model_fields_set and employee.category is not None:
            raise HTTPException(status_code=400, detail="Категории доступны только для ролей")
        employee.category = None

    assignments = None
    if isinstance(session, AsyncSession):
        if position_changed:
            await _close_invalid_assignments_for_position(session, employee, date.today())
        if {"category", "default_cooking_station", "position"} & patch.model_fields_set:
            await employee_assignment_service.sync_primary_from_shortcut(
                session,
                employee,
                commit=False,
            )
        assignments = await employee_assignment_service.get_assignments(
            session,
            employee.id,
            date.today(),
        )

    employee.is_senior, employee.is_deputy_senior = reset_inapplicable_premiums(
        employee.position,
        is_senior=employee.is_senior,
        is_deputy_senior=employee.is_deputy_senior,
    )
    employee.status = compute_status(
        employee,
        is_iiko_deleted=is_iiko_deleted,
        position_group=position_group_for_position(employee.position),
        assignments=assignments,
    )
    now = datetime.now(UTC)
    employee.updated_at = now
    after = _employee_lifecycle_snapshot(employee)
    if before != after:
        await _add_employee_lifecycle_action(
            session,
            action_type="update",
            employee=employee,
            before=before,
            after=after,
            now=now,
            actor=actor,
        )

    await session.commit()
    await session.refresh(employee)
    if isinstance(session, AsyncSession):
        return await _get_employee_or_404(session, employee_id, include_assignments=True)
    return employee


async def _get_employee_or_404(
    session: AsyncSession,
    employee_id: uuid.UUID,
    *,
    include_assignments: bool = False,
) -> Employee:
    if include_assignments and isinstance(session, AsyncSession):
        employee = await session.scalar(
            select(Employee)
            .options(selectinload(Employee.role_assignments))
            .where(Employee.id == employee_id)
        )
    else:
        employee = await session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    return employee


async def _get_assignment_or_404(
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")
    return assignment


def _active_assignment_exists(
    on_date: date,
    *,
    category: str | None = None,
    payroll_role: str | None = None,
) -> Any:
    assignment_query = select(EmployeeRoleAssignment.id).where(
        EmployeeRoleAssignment.employee_id == Employee.id,
        EmployeeRoleAssignment.effective_from <= on_date,
        or_(
            EmployeeRoleAssignment.effective_to.is_(None),
            EmployeeRoleAssignment.effective_to > on_date,
        ),
    )
    if category is not None:
        assignment_query = assignment_query.where(EmployeeRoleAssignment.category == category)
    if payroll_role is not None:
        assignment_query = assignment_query.where(
            EmployeeRoleAssignment.payroll_role == payroll_role
        )
    return assignment_query.exists()


def _normalize_patch_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    if "cooking_station" in normalized:
        if "default_cooking_station" in normalized:
            raise HTTPException(
                status_code=400,
                detail="Use either cooking_station or default_cooking_station",
            )
        normalized["default_cooking_station"] = normalized.pop("cooking_station")
    return normalized


def _validate_patch_payload(payload: dict[str, Any]) -> None:
    read_only = READ_ONLY_FIELDS & payload.keys()
    if "full_name" in read_only:
        raise HTTPException(status_code=400, detail="full_name is synchronized from iiko")
    if read_only:
        raise HTTPException(
            status_code=400,
            detail=f"Read-only fields: {', '.join(sorted(read_only))}",
        )

    computed = COMPUTED_FIELDS & payload.keys()
    if computed:
        raise HTTPException(
            status_code=400,
            detail=f"Computed fields: {', '.join(sorted(computed))}",
        )

    unknown = set(payload) - APP_MANAGED_FIELDS
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported fields: {', '.join(sorted(unknown))}",
        )

    category_value = payload.get("category")
    if category_value is not None and category_value not in EMPLOYEE_CATEGORIES:
        raise HTTPException(status_code=400, detail="Invalid employee category")

    position_value = payload.get("position")
    if position_value is not None and canonical_position_name(position_value) is None:
        raise HTTPException(status_code=400, detail="Должность не входит в канонический список")

    station_value = payload.get("default_cooking_station")
    if station_value is not None and station_value not in COOKING_STATIONS:
        raise HTTPException(status_code=400, detail="Invalid cooking station")


async def _resolve_create_iiko_position(
    session: AsyncSession,
    iiko_role_id: str,
) -> IikoEmployeeRole:
    iiko_roles = await get_iiko_employee_roles(session)
    role_by_id = {role.id: role for role in iiko_roles}
    iiko_role = role_by_id.get(iiko_role_id)
    if iiko_role is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Выбранная должность iiko недоступна",
        )
    if not is_create_position(iiko_role.name):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Выбранная должность iiko недоступна для создания сотрудника",
        )
    return iiko_role


async def _resolve_create_roles(
    session: AsyncSession,
    payload: EmployeeCreateRequest,
    *,
    position: str,
) -> list[ResolvedCreateRole]:
    allowed_roles = payroll_roles_for_position(position)
    if not allowed_roles:
        if payload.roles:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Для выбранной должности роли не предусмотрены",
            )
        return []
    if not payload.roles:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Для выбранной должности укажите основную роль",
        )

    resolved: list[ResolvedCreateRole] = []
    for requested_role in payload.roles:
        payroll_role = requested_role.payroll_role
        if payroll_role not in ALLOWED_CREATE_PAYROLL_ROLES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Выбранная роль недоступна для создания сотрудника",
            )
        if payroll_role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Роль не соответствует выбранной должности iiko",
            )
        if requested_role.category not in categories_for_payroll_role(payroll_role):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Категория недоступна для этой роли",
            )
        try:
            await employee_assignment_service.ensure_category_available(
                session,
                payroll_role,
                requested_role.category,
            )
        except employee_assignment_service.EmployeeAssignmentError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        resolved.append(
            ResolvedCreateRole(
                payroll_role=payroll_role,
                category=requested_role.category,
                is_primary=requested_role.is_primary,
            )
        )
    return resolved


def _normalized_create_position_name(value: str) -> str:
    return value.replace("\xa0", " ").strip().replace("Ё", "Е").replace("ё", "е").casefold()


def _payroll_role_matches_create_position(payroll_role: str, position_group: str | None) -> bool:
    if position_group == "cook":
        return payroll_role in employee_assignment_service.COOK_PAYROLL_ROLES
    if position_group == "cashier":
        return payroll_role == "administrator"
    return False


def _validate_premium_flags(
    position: str | None,
    *,
    is_senior: bool,
    is_deputy_senior: bool,
    status_code: int = status.HTTP_400_BAD_REQUEST,
) -> None:
    try:
        validate_premiums(
            position,
            is_senior=is_senior,
            is_deputy_senior=is_deputy_senior,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


def _validate_explicit_premium_patch(
    patch: EmployeePatch,
    position: str | None,
) -> None:
    if "is_senior" in patch.model_fields_set and patch.is_senior:
        _validate_premium_flags(
            position,
            is_senior=True,
            is_deputy_senior=False,
        )
    if "is_deputy_senior" in patch.model_fields_set and patch.is_deputy_senior:
        _validate_premium_flags(
            position,
            is_senior=False,
            is_deputy_senior=True,
        )


async def _close_invalid_assignments_for_position(
    session: AsyncSession,
    employee: Employee,
    as_of: date,
) -> None:
    allowed_roles = set(payroll_roles_for_position(employee.position))
    assignments = await employee_assignment_service.get_assignments(session, employee.id, as_of)
    for assignment in assignments:
        if assignment.payroll_role in allowed_roles:
            continue
        assignment.is_primary = False
        assignment.effective_to = as_of

    remaining = [
        assignment
        for assignment in assignments
        if assignment.payroll_role in allowed_roles
        and assignment.effective_from <= as_of
        and (assignment.effective_to is None or assignment.effective_to > as_of)
    ]
    if remaining and not any(assignment.is_primary for assignment in remaining):
        remaining[0].is_primary = True

    primary = next((assignment for assignment in remaining if assignment.is_primary), None)
    if primary is None:
        employee.category = None
        employee.default_cooking_station = None
        return
    employee.category = primary.category
    employee.default_cooking_station = (
        primary.payroll_role if primary.payroll_role in COOKING_STATIONS else None
    )


def _require_owner(actor: CurrentActor) -> None:
    if actor.roles & OWNER_ONLY:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")


def _employee_lifecycle_snapshot(employee: Employee) -> dict[str, Any]:
    return {
        "id": str(employee.id),
        "iiko_id": employee.iiko_id,
        "full_name": employee.full_name,
        "position": employee.position,
        "category": employee.category,
        "default_cooking_station": employee.default_cooking_station,
        "is_senior": employee.is_senior,
        "is_deputy_senior": employee.is_deputy_senior,
        "status": employee.status,
        "hire_date": employee.hire_date.isoformat() if employee.hire_date else None,
        "fire_date": employee.fire_date.isoformat() if employee.fire_date else None,
        "fire_reason": employee.fire_reason,
        "pin_set_at": employee.pin_set_at.isoformat() if employee.pin_set_at else None,
        "iiko_sync_at": employee.iiko_sync_at.isoformat() if employee.iiko_sync_at else None,
    }


def _employee_assignment_snapshot(
    assignment: EmployeeRoleAssignment,
) -> dict[str, Any]:
    return _assignment_snapshot(assignment)


def _assignment_snapshot(assignment: EmployeeRoleAssignment) -> dict[str, Any]:
    return {
        "id": str(assignment.id),
        "employee_id": str(assignment.employee_id),
        "payroll_role": assignment.payroll_role,
        "category": assignment.category,
        "is_primary": assignment.is_primary,
        "effective_from": assignment.effective_from.isoformat(),
        "effective_to": assignment.effective_to.isoformat() if assignment.effective_to else None,
    }


async def _add_employee_lifecycle_action(
    session: AsyncSession,
    *,
    action_type: str,
    employee: Employee,
    before: dict[str, Any] | None,
    after: dict[str, Any],
    now: datetime,
    actor: CurrentActor,
    reason: str | None = None,
) -> uuid.UUID:
    agent_run = AgentRun(
        id=uuid.uuid4(),
        agent_name="employee_lifecycle_manual",
        finished_at=now,
        status="success",
        params={
            "employee_id": str(employee.id),
            "actor_roles": sorted(actor.roles),
            "reason": reason,
        },
        result={"action_type": action_type},
    )
    session.add(agent_run)
    await session.flush()
    session.add(
        AgentAction(
            id=uuid.uuid4(),
            agent_run_id=agent_run.id,
            action_type=action_type,
            target_table="employee",
            target_id=employee.id,
            before_value=before,
            after_value=after,
        )
    )
    return agent_run.id


async def _add_manual_action(
    session: AsyncSession,
    *,
    action_type: str,
    target_table: str,
    target_id: uuid.UUID,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    now: datetime,
    actor: CurrentActor,
    employee_id: uuid.UUID | None = None,
) -> uuid.UUID:
    agent_run = AgentRun(
        id=uuid.uuid4(),
        agent_name="employee_manual_change",
        finished_at=now,
        status="success",
        params={
            "employee_id": str(employee_id) if employee_id else None,
            "actor_roles": sorted(actor.roles),
        },
        result={"action_type": action_type},
    )
    session.add(agent_run)
    await session.flush()
    session.add(
        AgentAction(
            id=uuid.uuid4(),
            agent_run_id=agent_run.id,
            action_type=action_type,
            target_table=target_table,
            target_id=target_id,
            before_value=before,
            after_value=after,
        )
    )
    return agent_run.id


async def _close_active_shift_entries_on_dismiss(
    session: AsyncSession,
    employee: Employee,
    *,
    now: datetime,
    agent_run_id: uuid.UUID,
) -> None:
    result = await session.scalars(
        select(ShiftLedgerEntry).where(
            ShiftLedgerEntry.employee_id == employee.id,
            ShiftLedgerEntry.closed_at.is_(None),
        )
    )
    for entry in result.all():
        before = ledger_entry_snapshot(entry)
        entry.closed_at = now
        notes = [entry.notes] if entry.notes else []
        notes.append("Закрыто при увольнении сотрудника")
        entry.notes = "; ".join(notes)
        after = ledger_entry_snapshot(entry)
        session.add(
            AgentAction(
                id=uuid.uuid4(),
                agent_run_id=agent_run_id,
                action_type="close_active_shift_on_dismiss",
                target_table="shift_ledger_entry",
                target_id=entry.id,
                before_value=before,
                after_value=after,
            )
        )

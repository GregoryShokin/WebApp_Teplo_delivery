from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentActor, get_current_actor, require_finance_manager_plus
from app.db.session import get_session
from app.models import Employee, EmployeeRoleAssignment
from app.schemas.employees import (
    EmployeePatch,
    EmployeeRead,
    EmployeeRoleAssignmentCreate,
    EmployeeRoleAssignmentPatch,
    EmployeeRoleAssignmentRead,
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
from app.services.iiko_sync import sync_employees

router = APIRouter()

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
    result = await sync_employees(session, run_reason="manual", mode=mode)
    return result.as_dict()


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
    try:
        return await employee_assignment_service.add_role(
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
    try:
        return await employee_assignment_service.update_assignment(
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
    try:
        await employee_assignment_service.remove_assignment(session, employee_id, assignment_id)
    except employee_assignment_service.EmployeeAssignmentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except employee_assignment_service.EmployeeAssignmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    is_iiko_deleted = employee.status == "inactive"

    for field in patch.model_fields_set:
        setattr(employee, field, getattr(patch, field))

    if not is_cook_position(employee.position):
        if "default_cooking_station" in patch.model_fields_set and employee.default_cooking_station:
            raise HTTPException(status_code=400, detail="Цех допустим только для поваров")
        employee.default_cooking_station = None

    assignments = None
    if isinstance(session, AsyncSession):
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

    employee.status = compute_status(
        employee,
        is_iiko_deleted=is_iiko_deleted,
        position_group=position_group_for_position(employee.position),
        assignments=assignments,
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

    station_value = payload.get("default_cooking_station")
    if station_value is not None and station_value not in COOKING_STATIONS:
        raise HTTPException(status_code=400, detail="Invalid cooking station")

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_finance_manager_plus
from app.db.session import get_session
from app.models import Employee
from app.schemas.employees import EmployeePatch, EmployeeRead, SyncResultRead
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
    query = select(Employee)
    if status_filter:
        if status_filter not in EMPLOYEE_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid employee status")
        query = query.where(Employee.status == status_filter)
    if category:
        if category not in EMPLOYEE_CATEGORIES:
            raise HTTPException(status_code=400, detail="Invalid employee category")
        query = query.where(Employee.category == category)
    if cooking_station:
        if cooking_station not in COOKING_STATIONS:
            raise HTTPException(status_code=400, detail="Invalid cooking station")
        query = query.where(Employee.default_cooking_station == cooking_station)
    if search:
        query = query.where(Employee.full_name.ilike(f"%{search}%"))

    result = await session.scalars(query.order_by(Employee.full_name))
    return list(result.all())


@router.get("/{employee_id}", response_model=EmployeeRead)
async def get_employee(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Employee:
    return await _get_employee_or_404(session, employee_id)


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

    employee.status = compute_status(
        employee,
        is_iiko_deleted=is_iiko_deleted,
        position_group=position_group_for_position(employee.position),
    )

    await session.commit()
    await session.refresh(employee)
    return employee


@router.post("/sync", response_model=SyncResultRead)
async def trigger_employee_sync(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    mode: Annotated[Literal["incremental", "reset"], Query()] = "incremental",
) -> dict[str, int]:
    require_finance_manager_plus(actor)
    result = await sync_employees(session, run_reason="manual", mode=mode)
    return result.as_dict()


async def _get_employee_or_404(session: AsyncSession, employee_id: uuid.UUID) -> Employee:
    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    return employee


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

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_finance_manager_plus
from app.db.session import get_session
from app.models import Employee
from app.schemas.employees import EmployeePatch, EmployeeRead, SyncResultRead
from app.services.iiko_sync import sync_employees

router = APIRouter()

EMPLOYEE_STATUSES = {"active", "inactive", "needs_setup"}
READ_ONLY_FIELDS = {"id", "full_name", "iiko_id", "iiko_sync_at", "created_at", "updated_at"}
APP_MANAGED_FIELDS = {
    "position",
    "category",
    "is_senior",
    "is_deputy_senior",
    "status",
    "hire_date",
    "fire_date",
}


@router.get("", response_model=list[EmployeeRead])
@router.get("/", response_model=list[EmployeeRead], include_in_schema=False)
async def list_employees(
    session: Annotated[AsyncSession, Depends(get_session)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    category: str | None = None,
    search: str | None = None,
) -> list[Employee]:
    query = select(Employee)
    if status_filter:
        if status_filter not in EMPLOYEE_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid employee status")
        query = query.where(Employee.status == status_filter)
    if category:
        query = query.where(Employee.category == category)
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
    _validate_patch_payload(payload)

    patch = EmployeePatch.model_validate(payload)
    employee = await _get_employee_or_404(session, employee_id)

    for field in patch.model_fields_set:
        setattr(employee, field, getattr(patch, field))

    if employee.status not in EMPLOYEE_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid employee status")

    _recalculate_setup_status(employee)

    await session.commit()
    await session.refresh(employee)
    return employee


@router.post("/sync", response_model=SyncResultRead)
async def trigger_employee_sync(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, int]:
    require_finance_manager_plus(actor)
    result = await sync_employees(session, run_reason="manual")
    return result.as_dict()


async def _get_employee_or_404(session: AsyncSession, employee_id: uuid.UUID) -> Employee:
    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    return employee


def _validate_patch_payload(payload: dict[str, Any]) -> None:
    read_only = READ_ONLY_FIELDS & payload.keys()
    if "full_name" in read_only:
        raise HTTPException(status_code=400, detail="full_name is synchronized from iiko")
    if read_only:
        raise HTTPException(
            status_code=400,
            detail=f"Read-only fields: {', '.join(sorted(read_only))}",
        )

    unknown = set(payload) - APP_MANAGED_FIELDS
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported fields: {', '.join(sorted(unknown))}",
        )

    status_value = payload.get("status")
    if status_value is not None and status_value not in EMPLOYEE_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid employee status")


def _recalculate_setup_status(employee: Employee) -> None:
    if employee.status == "inactive":
        return
    employee.status = "active" if employee.position and employee.category else "needs_setup"

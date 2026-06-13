"""API авансов и займов сотрудников.

Доступное к авансу, выдача (классификация аванс/заём по правам инициатора),
отмена, списание и реестр. Маршрутизация прав по пайплайну: админ-должности →
`payroll.advances.admin.issue`, прочие → `payroll.advances.production.issue`;
выдача сверх заработанного (заём) требует `payroll.loans.issue` (право B).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentActor,
    ensure_any_permission,
    ensure_permission,
    get_current_actor,
    require_permission,
)
from app.auth.permissions import permission_is_granted
from app.db.session import get_session
from app.models import Employee
from app.services.employee_effective_events import get_position_on_date
from app.services.payroll_advance_availability import AdvanceAvailability, available_to_advance
from app.services.payroll_advance_service import (
    cancel_advance,
    get_loan_max,
    issue_advance,
    list_advances,
    set_loan_max,
    write_off_advance,
)
from app.services.payroll_runner import PayrollConflictError, PayrollNotFoundError
from app.services.position_registry import admin_payroll_positions

router = APIRouter()

ADVANCES_READ_ACCESS = (Depends(require_permission("payroll.advances.read")),)
LOAN_ISSUE_ACCESS = (Depends(require_permission("payroll.loans.issue")),)

_ISSUE_ADMIN = "payroll.advances.admin.issue"
_ISSUE_PRODUCTION = "payroll.advances.production.issue"
_LOAN_ISSUE = "payroll.loans.issue"
_ISSUE_CODES = (_ISSUE_ADMIN, _ISSUE_PRODUCTION)


class AdvanceAvailabilityRead(BaseModel):
    employee_id: uuid.UUID
    as_of: date
    period_start: date | None
    period_end: date | None
    basis: str
    earned_to_date: float
    already_advanced: float
    available: float
    note: str | None = None


class AdvanceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    role: str
    kind: str
    amount: float
    per_installment_amount: float
    installments_count: int
    recovered_amount: float
    status: str
    issued_on: date
    payout_method: str | None = None
    comment: str | None = None


class AdvanceIssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_id: uuid.UUID
    amount: Decimal = Field(gt=0)
    issued_on: date | None = None
    payout_method: str | None = None
    installments_count: int = Field(default=1, ge=1)
    comment: str | None = None
    # Превышение потолка займа (требует права B + подтверждения в UI).
    override_ceiling: bool = False


class WriteOffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


class AdvanceConfigRead(BaseModel):
    loan_max: float


class AdvanceConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loan_max: Decimal = Field(gt=0)


def _availability_read(av: AdvanceAvailability) -> AdvanceAvailabilityRead:
    return AdvanceAvailabilityRead(
        employee_id=av.employee_id,
        as_of=av.as_of,
        period_start=av.period_start,
        period_end=av.period_end,
        basis=av.basis,
        earned_to_date=float(av.earned_to_date),
        already_advanced=float(av.already_advanced),
        available=float(av.available),
        note=av.note,
    )


async def _require_employee(session: AsyncSession, employee_id: uuid.UUID) -> Employee:
    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Сотрудник не найден")
    return employee


@router.get(
    "/availability",
    response_model=AdvanceAvailabilityRead,
    dependencies=ADVANCES_READ_ACCESS,
)
async def get_advance_availability(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    as_of: date | None = None,
) -> AdvanceAvailabilityRead:
    employee = await _require_employee(session, employee_id)
    availability = await available_to_advance(
        session, employee, as_of or datetime.now(UTC).date()
    )
    return _availability_read(availability)


@router.get("", response_model=list[AdvanceRead], dependencies=ADVANCES_READ_ACCESS)
async def get_advances(
    session: Annotated[AsyncSession, Depends(get_session)],
    employee_id: uuid.UUID | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> list[AdvanceRead]:
    statuses = (
        tuple(part.strip() for part in status_filter.split(",") if part.strip())
        if status_filter
        else None
    )
    rows = await list_advances(session, employee_id=employee_id, statuses=statuses)
    return [AdvanceRead.model_validate(row) for row in rows]


@router.post("", response_model=AdvanceRead, status_code=status.HTTP_201_CREATED)
async def post_advance(
    payload: AdvanceIssueRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> AdvanceRead:
    employee = await _require_employee(session, payload.employee_id)
    issued_on = payload.issued_on or datetime.now(UTC).date()
    position = await get_position_on_date(session, employee.id, issued_on)
    position = position or employee.position or ""
    issue_code = _ISSUE_ADMIN if position in admin_payroll_positions() else _ISSUE_PRODUCTION
    ensure_permission(actor, issue_code)
    allow_loan = permission_is_granted(_LOAN_ISSUE, actor.permissions)
    try:
        advance = await issue_advance(
            session,
            employee_id=payload.employee_id,
            amount=payload.amount,
            allow_loan=allow_loan,
            override_ceiling=payload.override_ceiling,
            issued_on=issued_on,
            payout_method=payload.payout_method,
            installments_count=payload.installments_count,
            comment=payload.comment,
            actor_user_id=actor.user_id,
        )
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return AdvanceRead.model_validate(advance)


@router.post("/{advance_id}/cancel", response_model=AdvanceRead)
async def post_cancel_advance(
    advance_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> AdvanceRead:
    ensure_any_permission(actor, _ISSUE_CODES)
    try:
        advance = await cancel_advance(session, advance_id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return AdvanceRead.model_validate(advance)


@router.post(
    "/{advance_id}/write-off",
    response_model=AdvanceRead,
    dependencies=LOAN_ISSUE_ACCESS,
)
async def post_write_off_advance(
    advance_id: uuid.UUID,
    payload: WriteOffRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AdvanceRead:
    try:
        advance = await write_off_advance(session, advance_id, reason=payload.reason)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return AdvanceRead.model_validate(advance)


@router.get("/config", response_model=AdvanceConfigRead, dependencies=ADVANCES_READ_ACCESS)
async def get_advance_config(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AdvanceConfigRead:
    return AdvanceConfigRead(loan_max=float(await get_loan_max(session)))


@router.put("/config", response_model=AdvanceConfigRead, dependencies=LOAN_ISSUE_ACCESS)
async def put_advance_config(
    payload: AdvanceConfigUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AdvanceConfigRead:
    try:
        loan_max = await set_loan_max(session, payload.loan_max)
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return AdvanceConfigRead(loan_max=float(loan_max))

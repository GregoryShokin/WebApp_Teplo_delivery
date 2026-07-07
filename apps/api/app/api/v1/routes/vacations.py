from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_permission
from app.db.session import get_session
from app.models import Employee, User, VacationPeriod
from app.schemas.vacations import (
    VacationBalanceRead,
    VacationConflictResponse,
    VacationPeriodCreate,
    VacationPeriodPatch,
    VacationPeriodRead,
    VacationRosterRow,
)
from app.services import vacation_service
from app.services.position_registry import vacation_positions
from app.services.vacation_service import UNSET, VacationShiftConflictError

router = APIRouter()
VACATIONS_READ_ACCESS = (Depends(require_permission("payroll.vacations.read")),)
VACATIONS_EDIT_ACCESS = (Depends(require_permission("payroll.vacations.edit")),)


@router.get("", response_model=list[VacationPeriodRead], dependencies=VACATIONS_READ_ACCESS)
@router.get(
    "/",
    response_model=list[VacationPeriodRead],
    include_in_schema=False,
    dependencies=VACATIONS_READ_ACCESS,
)
async def get_vacations(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    employee_id: uuid.UUID | None = None,
    year: int | None = None,
    include_cancelled: bool = False,
) -> list[VacationPeriodRead]:
    del actor
    periods = await vacation_service.list_vacations(
        session,
        employee_id=employee_id,
        year=year,
        include_cancelled=include_cancelled,
    )
    return await _periods_to_read(session, periods)


@router.get("/balance", response_model=VacationBalanceRead, dependencies=VACATIONS_READ_ACCESS)
async def get_vacation_balance(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    employee_id: uuid.UUID,
    year: int,
) -> VacationBalanceRead:
    del actor
    balance = await vacation_service.get_vacation_balance(
        session,
        employee_id=employee_id,
        year=year,
    )
    periods = await _periods_to_read(session, balance.periods)
    return VacationBalanceRead(
        employee_id=balance.employee_id,
        year=balance.year,
        limit=balance.limit,
        used=balance.used,
        remaining=balance.remaining,
        periods=periods,
    )


@router.get("/roster", response_model=list[VacationRosterRow], dependencies=VACATIONS_READ_ACCESS)
async def get_vacation_roster(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    year: Annotated[int | None, Query()] = None,
) -> list[VacationRosterRow]:
    del actor
    target_year = year or date.today().year
    employees = (
        await session.scalars(
            select(Employee)
            # Плейсхолдеры пула «Внештат №N» в отпускной реестр не попадают.
            .where(
                Employee.position.in_(vacation_positions()),
                Employee.status == "active",
                Employee.is_freelancer_placeholder.is_(False),
            )
            .order_by(Employee.full_name)
        )
    ).all()
    rows: list[VacationRosterRow] = []
    for employee in employees:
        if employee.position not in vacation_positions() or employee.status != "active":
            continue
        balance = await vacation_service.get_vacation_balance(
            session,
            employee_id=employee.id,
            year=target_year,
        )
        periods = await _periods_to_read(session, balance.periods)
        rows.append(
            VacationRosterRow(
                employee_id=employee.id,
                employee_full_name=employee.full_name,
                position=employee.position,
                year=target_year,
                limit=balance.limit,
                used=balance.used,
                remaining=balance.remaining,
                periods=periods,
            )
        )
    return rows


@router.get("/payout-dates", response_model=list[date], dependencies=VACATIONS_READ_ACCESS)
async def get_vacation_payout_dates(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[date]:
    del actor
    return await vacation_service.eligible_payout_dates(session)


@router.post("", response_model=VacationPeriodRead, dependencies=VACATIONS_EDIT_ACCESS)
@router.post(
    "/",
    response_model=VacationPeriodRead,
    include_in_schema=False,
    dependencies=VACATIONS_EDIT_ACCESS,
)
async def post_vacation(
    payload: VacationPeriodCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> VacationPeriodRead | JSONResponse:
    try:
        period = await vacation_service.create_vacation_period(
            session,
            employee_id=payload.employee_id,
            date_start=payload.date_start,
            date_end=payload.date_end,
            comment=payload.comment,
            actor=actor,
            payout_date=payload.payout_date,
            force_remove_conflicting_shifts=payload.force_remove_conflicting_shifts,
        )
    except VacationShiftConflictError as exc:
        return _conflict_response(exc)
    return (await _periods_to_read(session, [period]))[0]


@router.patch(
    "/{period_id}",
    response_model=VacationPeriodRead,
    dependencies=VACATIONS_EDIT_ACCESS,
)
async def patch_vacation(
    period_id: uuid.UUID,
    payload: VacationPeriodPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> VacationPeriodRead | JSONResponse:
    comment = payload.comment if "comment" in payload.model_fields_set else UNSET
    payout_date = payload.payout_date if "payout_date" in payload.model_fields_set else UNSET
    try:
        period = await vacation_service.update_vacation_period(
            session,
            period_id,
            date_start=payload.date_start,
            date_end=payload.date_end,
            comment=comment,
            payout_date=payout_date,
            actor=actor,
            force_remove_conflicting_shifts=payload.force_remove_conflicting_shifts,
        )
    except VacationShiftConflictError as exc:
        return _conflict_response(exc)
    return (await _periods_to_read(session, [period]))[0]


@router.post(
    "/{period_id}/cancel",
    response_model=VacationPeriodRead,
    dependencies=VACATIONS_EDIT_ACCESS,
)
async def post_vacation_cancel(
    period_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> VacationPeriodRead:
    period = await vacation_service.cancel_vacation_period(session, period_id, actor=actor)
    return (await _periods_to_read(session, [period]))[0]


async def _periods_to_read(
    session: AsyncSession,
    periods: list[VacationPeriod],
) -> list[VacationPeriodRead]:
    if not periods:
        return []
    employee_ids = {period.employee_id for period in periods}
    user_ids = {
        period.created_by_user_id for period in periods if period.created_by_user_id is not None
    }
    employees = {
        employee.id: employee
        for employee in (
            await session.scalars(select(Employee).where(Employee.id.in_(employee_ids)))
        ).all()
    }
    users = {}
    if user_ids:
        users = {
            user.id: user
            for user in (await session.scalars(select(User).where(User.id.in_(user_ids)))).all()
        }
    daily_amount = await vacation_service.vacation_daily_amount(session)
    return [
        VacationPeriodRead(
            id=period.id,
            employee_id=period.employee_id,
            employee_full_name=employees.get(period.employee_id).full_name
            if period.employee_id in employees
            else str(period.employee_id),
            date_start=period.date_start,
            date_end=period.date_end,
            days_count=period.days_count,
            payout_date=period.payout_date,
            payout_amount=(daily_amount * period.days_count) if period.payout_date else None,
            status=period.status,
            comment=period.comment,
            created_by_label=_user_label(users.get(period.created_by_user_id)),
            created_at=period.created_at,
        )
        for period in periods
    ]


def _conflict_response(exc: VacationShiftConflictError) -> JSONResponse:
    payload = VacationConflictResponse(
        detail="На период отпуска уже есть смены",
        conflicting_shifts=vacation_service.shift_conflicts_to_dicts(exc.conflicts),
    )
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=payload.model_dump(mode="json"),
    )


def _user_label(user: User | None) -> str | None:
    if user is None:
        return None
    return user.full_name or user.email

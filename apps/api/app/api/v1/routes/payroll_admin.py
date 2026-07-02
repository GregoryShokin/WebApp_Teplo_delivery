"""API ведомостей ЗП административного персонала (полумесячные периоды).

Список/создание/просмотр админских ведомостей. Финализация, отметки выплат и
банковский черновик переиспользуют существующие эндпоинты `/payroll/runs/{id}/...`
(они работают по run_id и не зависят от типа периода), поэтому здесь не дублируются.

Права на MVP переиспользуют коды производственных ведомостей `payroll.runs.*`.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, ensure_permission, get_current_actor, require_permission
from app.api.v1.routes.payroll import (
    get_deposit_payouts_by_employee,
    get_payments_by_employee,
    line_is_on_demand,
    serialize_payroll_line,
)
from app.db.session import get_session
from app.schemas.payroll import (
    PayrollLineRead,
    PayrollPeriodRead,
    PayrollRunCreate,
    PayrollRunRead,
)
from app.services.payroll_admin import (
    auto_create_next_admin_period,
    clear_admin_oklad_override,
    compute_on_demand_debt,
    get_dishwasher_pool,
    list_admin_oklady,
    list_admin_runs,
    list_dishwasher_employees,
    list_dishwasher_shifts,
    run_admin_payroll,
    set_admin_payroll_exclusion,
    set_dishwasher_pool,
    set_dishwasher_shift,
    set_okladnik_payout_mode,
    upsert_admin_oklad,
)
from app.services.payroll_runner import (
    PayrollConflictError,
    PayrollNotFoundError,
    get_run,
    get_run_lines,
    serialize_period,
    serialize_run,
)

router = APIRouter()

PAYROLL_RUNS_READ_ACCESS = (Depends(require_permission("payroll.runs.read")),)
PAYROLL_ADMIN_START_ACCESS = (Depends(require_permission("payroll.runs.admin.start")),)
SOURCE_RATES_READ_ACCESS = (Depends(require_permission("source.rates.read")),)
SOURCE_RATES_EDIT_ACCESS = (Depends(require_permission("source.rates.edit")),)
SOURCE_SCHEDULE_READ_ACCESS = (Depends(require_permission("source.schedule.read")),)
DISHWASHER_EDIT_ACCESS = (Depends(require_permission("source.schedule.dishwashers.edit")),)


class AdminSalaryDefaultRead(BaseModel):
    position: str
    amount: float | None = None
    effective_from: date | None = None
    payout_mode: str = "split"


class AdminSalaryOverrideRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    employee_id: uuid.UUID
    employee_name: str | None = None
    position: str
    amount: float
    effective_from: date | None = None


class AdminSalariesRead(BaseModel):
    defaults: list[AdminSalaryDefaultRead]
    overrides: list[AdminSalaryOverrideRead]


class AdminSalaryUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: str
    amount: Decimal = Field(gt=0)
    effective_from: date


class AdminExclusionUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    excluded: bool


class AdminExclusionRead(BaseModel):
    employee_id: uuid.UUID
    admin_payroll_excluded: bool


class AdminPayoutModeUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str


class DishwasherPoolRead(BaseModel):
    pool: float


class DishwasherPoolUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pool: Decimal = Field(gt=0)


class DishwasherEmployeeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str


class DishwasherShiftRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    employee_id: uuid.UUID
    work_date: date


class DishwasherShiftUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_id: uuid.UUID
    work_date: date
    worked: bool


@router.post(
    "/periods/auto-create-next",
    response_model=PayrollPeriodRead,
    dependencies=PAYROLL_ADMIN_START_ACCESS,
)
async def post_auto_create_next_admin_period(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    period = await auto_create_next_admin_period(session)
    return serialize_period(period)


@router.get("/runs", response_model=list[PayrollRunRead], dependencies=PAYROLL_RUNS_READ_ACCESS)
async def get_admin_runs(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[dict]:
    return [serialize_run(run, period) for run, period in await list_admin_runs(session)]


@router.post("/runs", response_model=PayrollRunRead)
async def post_admin_run(
    payload: PayrollRunCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    ensure_permission(
        actor,
        "payroll.runs.admin.recalculate" if payload.force_refresh else "payroll.runs.admin.start",
    )
    period_id = payload.period_id
    if period_id is None:
        period = await auto_create_next_admin_period(session)
        period_id = period.id

    try:
        run = await run_admin_payroll(session, period_id)
        return await get_run(session, run.id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get(
    "/runs/{run_id}",
    response_model=PayrollRunRead,
    dependencies=PAYROLL_RUNS_READ_ACCESS,
)
async def get_admin_run_detail(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    try:
        return await get_run(session, run_id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get(
    "/runs/{run_id}/lines",
    response_model=list[PayrollLineRead],
    dependencies=PAYROLL_RUNS_READ_ACCESS,
)
async def get_admin_run_lines(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[PayrollLineRead]:
    try:
        lines = await get_run_lines(session, run_id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    payouts_by_employee = await get_deposit_payouts_by_employee(
        session, run_id, (line.employee_id for line in lines)
    )
    payments_by_employee = await get_payments_by_employee(
        session, run_id, (line.employee_id for line in lines)
    )
    on_demand_ids = [line.employee_id for line in lines if line_is_on_demand(line)]
    on_demand_debt_by_employee = (
        await compute_on_demand_debt(session, on_demand_ids) if on_demand_ids else {}
    )
    return [
        serialize_payroll_line(
            line, payouts_by_employee, payments_by_employee, on_demand_debt_by_employee
        )
        for line in lines
    ]


# --------------------------------------------------------------------------- #
# Оклады администрации
# --------------------------------------------------------------------------- #
@router.get("/salaries", response_model=AdminSalariesRead, dependencies=SOURCE_RATES_READ_ACCESS)
async def get_admin_salaries(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    return await list_admin_oklady(session)


@router.put("/salaries/default", response_model=AdminSalariesRead, dependencies=SOURCE_RATES_EDIT_ACCESS)
async def put_admin_salary_default(
    payload: AdminSalaryUpsert,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    try:
        await upsert_admin_oklad(
            session,
            position=payload.position,
            employee_id=None,
            amount=payload.amount,
            effective_from=payload.effective_from,
        )
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return await list_admin_oklady(session)


@router.put(
    "/salaries/override/{employee_id}",
    response_model=AdminSalariesRead,
    dependencies=SOURCE_RATES_EDIT_ACCESS,
)
async def put_admin_salary_override(
    employee_id: uuid.UUID,
    payload: AdminSalaryUpsert,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    try:
        await upsert_admin_oklad(
            session,
            position=payload.position,
            employee_id=employee_id,
            amount=payload.amount,
            effective_from=payload.effective_from,
        )
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return await list_admin_oklady(session)


@router.delete(
    "/salaries/override/{employee_id}",
    response_model=AdminSalariesRead,
    dependencies=SOURCE_RATES_EDIT_ACCESS,
)
async def delete_admin_salary_override(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    await clear_admin_oklad_override(session, employee_id)
    return await list_admin_oklady(session)


@router.put(
    "/salaries/exclusion/{employee_id}",
    response_model=AdminExclusionRead,
    dependencies=SOURCE_RATES_EDIT_ACCESS,
)
async def put_admin_salary_exclusion(
    employee_id: uuid.UUID,
    payload: AdminExclusionUpsert,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> AdminExclusionRead:
    try:
        employee = await set_admin_payroll_exclusion(session, employee_id, payload.excluded)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return AdminExclusionRead(
        employee_id=employee.id,
        admin_payroll_excluded=employee.admin_payroll_excluded,
    )


@router.put(
    "/salaries/payout-mode/{position}",
    response_model=AdminSalariesRead,
    dependencies=SOURCE_RATES_EDIT_ACCESS,
)
async def put_admin_payout_mode(
    position: str,
    payload: AdminPayoutModeUpsert,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    try:
        await set_okladnik_payout_mode(session, position, payload.mode)
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return await list_admin_oklady(session)


# --------------------------------------------------------------------------- #
# Мойщицы (посменно)
# --------------------------------------------------------------------------- #
@router.get(
    "/dishwasher/pool", response_model=DishwasherPoolRead, dependencies=SOURCE_RATES_READ_ACCESS
)
async def get_dishwasher_pool_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> DishwasherPoolRead:
    pool = await get_dishwasher_pool(session)
    return DishwasherPoolRead(pool=float(pool))


@router.put(
    "/dishwasher/pool", response_model=DishwasherPoolRead, dependencies=SOURCE_RATES_EDIT_ACCESS
)
async def put_dishwasher_pool_endpoint(
    payload: DishwasherPoolUpsert,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> DishwasherPoolRead:
    try:
        pool = await set_dishwasher_pool(session, payload.pool)
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return DishwasherPoolRead(pool=float(pool))


@router.get(
    "/dishwasher/employees",
    response_model=list[DishwasherEmployeeRead],
    dependencies=SOURCE_SCHEDULE_READ_ACCESS,
)
async def get_dishwasher_employees(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[DishwasherEmployeeRead]:
    employees = await list_dishwasher_employees(session)
    return [DishwasherEmployeeRead.model_validate(employee) for employee in employees]


@router.get(
    "/dishwasher/shifts",
    response_model=list[DishwasherShiftRead],
    dependencies=SOURCE_SCHEDULE_READ_ACCESS,
)
async def get_dishwasher_shifts(
    period_start: date,
    period_end: date,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[DishwasherShiftRead]:
    shifts = await list_dishwasher_shifts(
        session, period_start=period_start, period_end=period_end
    )
    return [DishwasherShiftRead.model_validate(shift) for shift in shifts]


@router.put(
    "/dishwasher/shifts",
    response_model=DishwasherShiftRead,
    dependencies=DISHWASHER_EDIT_ACCESS,
)
async def put_dishwasher_shift(
    payload: DishwasherShiftUpsert,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> DishwasherShiftRead:
    try:
        await set_dishwasher_shift(
            session,
            employee_id=payload.employee_id,
            work_date=payload.work_date,
            worked=payload.worked,
            actor_user_id=actor.user_id,
        )
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return DishwasherShiftRead(employee_id=payload.employee_id, work_date=payload.work_date)

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_finance_manager_plus
from app.db.session import get_session
from app.schemas.payroll import PayrollLineRead, PayrollPeriodRead, PayrollRunCreate, PayrollRunRead
from app.services.payroll_runner import (
    PayrollConflictError,
    PayrollNotFoundError,
    auto_create_next_period,
    finalize_payroll_run,
    get_run,
    get_run_lines,
    list_runs,
    run_payroll,
    serialize_period,
)

router = APIRouter()


@router.post("/periods/auto-create-next", response_model=PayrollPeriodRead)
async def post_auto_create_next_period(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    require_finance_manager_plus(actor)
    period = await auto_create_next_period(session)
    return serialize_period(period)


@router.get("/runs", response_model=list[PayrollRunRead])
async def get_runs(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[dict]:
    return await list_runs(session)


@router.post("/runs", response_model=PayrollRunRead)
async def post_run(
    payload: PayrollRunCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    require_finance_manager_plus(actor)
    period_id = payload.period_id
    if period_id is None:
        period = await auto_create_next_period(session)
        period_id = period.id

    try:
        run = await run_payroll(session, period_id)
        return await get_run(session, run.id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/runs/{run_id}", response_model=PayrollRunRead)
async def get_run_detail(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    try:
        return await get_run(session, run_id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/runs/{run_id}/lines", response_model=list[PayrollLineRead])
async def get_lines(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list:
    try:
        return await get_run_lines(session, run_id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/runs/{run_id}/finalize", response_model=PayrollRunRead)
async def post_finalize(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    require_finance_manager_plus(actor)
    try:
        run = await finalize_payroll_run(session, run_id)
        return await get_run(session, run.id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_manager_plus
from app.db.session import get_session
from app.schemas.payroll import (
    ShiftLedgerBuildRequest,
    ShiftLedgerEntryRead,
    ShiftLedgerMatrixRead,
    ShiftLedgerPatch,
)
from app.services.shift_ledger import (
    ShiftLedgerNotFoundError,
    ShiftLedgerValidationError,
    build_ledger_for_date,
    iter_dates,
    ledger_today,
    ledger_week_bounds,
    list_ledger_for_date,
    list_ledger_matrix,
    manually_correct,
)

router = APIRouter()


@router.get("/ledger", response_model=list[ShiftLedgerEntryRead])
async def get_shift_ledger(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    work_date: Annotated[date, Query(alias="date")],
) -> list[dict]:
    require_manager_plus(actor)
    return await list_ledger_for_date(session, work_date)


@router.get("/ledger/matrix", response_model=ShiftLedgerMatrixRead)
async def get_shift_ledger_matrix(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    selected_date: Annotated[date, Query(alias="date")],
) -> dict:
    require_manager_plus(actor)
    ensure_not_future(selected_date)
    return await list_ledger_matrix(session, selected_date)


@router.post("/ledger/build", response_model=list[ShiftLedgerEntryRead])
async def post_build_shift_ledger(
    payload: ShiftLedgerBuildRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[dict]:
    require_manager_plus(actor)
    await build_ledger_for_date(session, payload.work_date)
    return await list_ledger_for_date(session, payload.work_date)


@router.post("/ledger/build-week", response_model=ShiftLedgerMatrixRead)
async def post_build_shift_ledger_week(
    payload: ShiftLedgerBuildRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    require_manager_plus(actor)
    ensure_not_future(payload.work_date)
    start_date, end_date = ledger_week_bounds(payload.work_date)
    for work_date in iter_dates(start_date, end_date):
        await build_ledger_for_date(session, work_date)
    return await list_ledger_matrix(session, payload.work_date)


@router.patch("/ledger/{entry_id}", response_model=ShiftLedgerEntryRead)
async def patch_shift_ledger_entry(
    entry_id: uuid.UUID,
    payload: ShiftLedgerPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    require_manager_plus(actor)
    try:
        entry = await manually_correct(
            session,
            entry_id,
            payload.payroll_role,
        )
    except ShiftLedgerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ShiftLedgerValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    rows = await list_ledger_for_date(session, entry.work_date)
    return next(row for row in rows if row["id"] == str(entry.id))


def ensure_not_future(value: date) -> None:
    if value > ledger_today():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Нельзя выбирать будущую дату",
        )

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_permission
from app.db.session import get_session
from app.models import ShiftLedgerEntry
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
    get_latest_locked_payroll_date,
    is_payroll_locked,
    iter_dates,
    ledger_today,
    ledger_week_bounds,
    list_ledger_for_date,
    list_ledger_matrix,
    manually_correct,
)

router = APIRouter()
SHIFTS_READ_ACCESS = (Depends(require_permission("source.shift_ledger.read")),)
SHIFTS_INPUT_ACCESS = (Depends(require_permission("source.shift_ledger.input")),)
SHIFTS_CORRECT_ACCESS = (Depends(require_permission("source.shift_ledger.correct")),)


@router.get("/ledger", response_model=list[ShiftLedgerEntryRead], dependencies=SHIFTS_READ_ACCESS)
async def get_shift_ledger(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    work_date: Annotated[date, Query(alias="date")],
) -> list[dict]:
    return await list_ledger_for_date(session, work_date)


@router.get("/ledger/matrix", response_model=ShiftLedgerMatrixRead, dependencies=SHIFTS_READ_ACCESS)
async def get_shift_ledger_matrix(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    selected_date: Annotated[date, Query(alias="date")],
) -> dict:
    ensure_not_future(selected_date)
    return await list_ledger_matrix(session, selected_date)


@router.post(
    "/ledger/build",
    response_model=list[ShiftLedgerEntryRead],
    dependencies=SHIFTS_INPUT_ACCESS,
)
async def post_build_shift_ledger(
    payload: ShiftLedgerBuildRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[dict]:
    await build_ledger_for_date(session, payload.work_date)
    return await list_ledger_for_date(session, payload.work_date)


@router.post(
    "/ledger/build-week",
    response_model=ShiftLedgerMatrixRead,
    dependencies=SHIFTS_INPUT_ACCESS,
)
async def post_build_shift_ledger_week(
    payload: ShiftLedgerBuildRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    ensure_not_future(payload.work_date)
    start_date, end_date = ledger_week_bounds(payload.work_date)
    for work_date in iter_dates(start_date, end_date):
        await build_ledger_for_date(session, work_date)
    return await list_ledger_matrix(session, payload.work_date)


@router.patch(
    "/ledger/{entry_id}",
    response_model=ShiftLedgerEntryRead,
    dependencies=SHIFTS_CORRECT_ACCESS,
)
async def patch_shift_ledger_entry(
    entry_id: uuid.UUID,
    payload: ShiftLedgerPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    entry = await session.get(ShiftLedgerEntry, entry_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shift ledger entry not found",
        )

    latest_locked_date = await get_latest_locked_payroll_date(session)
    if is_payroll_locked(entry.work_date, latest_locked_date):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="ЗП за эту неделю уже закрыта, изменение роли невозможно",
        )

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

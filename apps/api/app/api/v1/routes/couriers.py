from __future__ import annotations

import http.client as _http_client
import uuid
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentActor,
    get_current_actor,
    require_finance_manager_plus,
    require_manager_plus,
)
from app.db.session import get_session
from app.services.courier_sync import (
    MOSCOW_TZ,
    list_courier_deliveries,
    list_courier_shifts,
    sync_courier_cold_backfill,
    sync_courier_deliveries,
    sync_courier_hot_window,
)

router = APIRouter()
CourierSyncMode = Literal["hot", "cold", "custom"]
MAX_DELIVERY_WINDOW_DAYS = 92


class CourierSyncRequest(BaseModel):
    mode: CourierSyncMode = "hot"
    date_from: date | None = None
    date_to: date | None = None


@router.post("/sync")
async def post_courier_sync(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    payload: Annotated[CourierSyncRequest | None, Body()] = None,
    mode: Annotated[CourierSyncMode | None, Query()] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
) -> dict[str, int]:
    require_finance_manager_plus(actor)
    resolved_mode = mode or (payload.mode if payload is not None else "hot")
    resolved_date_from = date_from if date_from is not None else (
        payload.date_from if payload is not None else None
    )
    resolved_date_to = date_to if date_to is not None else (
        payload.date_to if payload is not None else None
    )

    try:
        if resolved_mode == "hot":
            result = await sync_courier_hot_window(session)
        elif resolved_mode == "cold":
            result = await sync_courier_cold_backfill(session)
        else:
            if resolved_date_from is None or resolved_date_to is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="date_from and date_to are required for custom sync",
                )
            ensure_date_range(resolved_date_from, resolved_date_to)
            ensure_not_future(resolved_date_from)
            ensure_not_future(resolved_date_to)
            result = await sync_courier_deliveries(
                session,
                date_from=resolved_date_from,
                date_to=resolved_date_to,
                run_reason="manual",
            )
    except _http_client.IncompleteRead as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="iiko не отвечает — сервер оборвал соединение. Попробуйте через минуту.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return result.as_dict()


@router.get("/deliveries")
async def get_courier_deliveries(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    date_from: Annotated[date, Query()],
    date_to: Annotated[date, Query()],
    courier_iiko_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(gt=0, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    require_manager_plus(actor)
    ensure_date_range(date_from, date_to)
    ensure_window(date_from, date_to, max_days=MAX_DELIVERY_WINDOW_DAYS)
    return await list_courier_deliveries(
        session,
        date_from=date_from,
        date_to=date_to,
        courier_iiko_id=courier_iiko_id,
        limit=limit,
        offset=offset,
    )


@router.get("/shifts")
async def get_courier_shifts(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    date_from: Annotated[date, Query()],
    date_to: Annotated[date, Query()],
    employee_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[dict[str, Any]]:
    require_manager_plus(actor)
    ensure_date_range(date_from, date_to)
    ensure_window(date_from, date_to, max_days=MAX_DELIVERY_WINDOW_DAYS)
    return await list_courier_shifts(
        session,
        date_from=date_from,
        date_to=date_to,
        employee_id=employee_id,
    )


def ensure_date_range(date_from: date, date_to: date) -> None:
    if date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date_from must be before or equal to date_to",
        )


def ensure_window(date_from: date, date_to: date, *, max_days: int) -> None:
    if (date_to - date_from) > timedelta(days=max_days - 1):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Окно не должно превышать {max_days} дня",
        )


def ensure_not_future(value: date) -> None:
    if value > datetime.now(MOSCOW_TZ).date():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Нельзя выбирать будущую дату",
        )

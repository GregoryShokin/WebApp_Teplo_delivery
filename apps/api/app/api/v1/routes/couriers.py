from __future__ import annotations

import http.client as _http_client
import uuid
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentActor,
    get_current_actor,
    require_finance_manager_plus,
    require_manager_plus,
)
from app.db.session import get_session
from app.models import CourierEvaluationCriterion, Employee
from app.schemas.couriers import (
    CourierCategoryAssignmentRead,
    CourierCategoryAssignRequest,
    CourierCategoryRow,
    CourierDepositCardRead,
    CourierDepositCategory,
    CourierDepositOpeningUpdate,
    CourierDepositRow,
    CourierDepositSettingsRead,
    CourierDepositSettingsUpdate,
    CourierDepositStatus,
    CourierDepositTransactionCreate,
    CourierDepositTransactionRead,
    CourierEvaluationCreate,
    CourierEvaluationCriterionRead,
    CourierEvaluationMonthlyAggregate,
    CourierEvaluationRead,
    CourierEvaluationUpdate,
    CourierScheduleEntryRead,
    CourierScheduleUpsert,
)
from app.services.courier_sync import (
    MOSCOW_TZ,
    list_courier_deliveries,
    list_courier_shifts,
    sync_courier_cold_backfill,
    sync_courier_deliveries,
    sync_courier_hot_window,
)
from app.services.couriers import (
    category_service,
    deposit_service,
    evaluation_service,
    schedule_service,
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


@router.get("/categories", response_model=list[CourierCategoryRow])
async def get_courier_categories(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    at_date: Annotated[date | None, Query()] = None,
) -> list[dict[str, Any]]:
    require_manager_plus(actor)
    result = await session.scalars(
        select(Employee)
        .where(Employee.position == "Курьер", Employee.status == "active")
        .order_by(Employee.full_name)
    )
    rows: list[dict[str, Any]] = []
    for employee in result.all():
        category = await category_service.get_current_category(session, employee.id, at_date)
        rows.append(
            {
                "employee_id": employee.id,
                "full_name": employee.full_name,
                "status": employee.status,
                "category": _enum_value(category),
            }
        )
    return rows


@router.post(
    "/{employee_id}/categories",
    response_model=CourierCategoryAssignmentRead,
)
async def post_courier_category(
    employee_id: uuid.UUID,
    payload: CourierCategoryAssignRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    require_manager_plus(actor)
    assignment = await category_service.assign_category(
        session,
        employee_id=employee_id,
        category=payload.category,
        effective_from=payload.effective_from,
        actor_id=payload.actor_id,
    )
    await session.commit()
    await session.refresh(assignment)
    return _category_assignment_payload(assignment)


@router.get("/deposits", response_model=list[CourierDepositRow])
async def get_courier_deposits(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    status_filter: Annotated[CourierDepositStatus, Query(alias="status")] = "active",
    category_filter: Annotated[CourierDepositCategory, Query(alias="category")] = "all",
) -> list[dict[str, Any]]:
    require_manager_plus(actor)
    rows = await deposit_service.list_couriers_with_balances(
        session,
        deposit_service.CourierDepositFilters(
            status=status_filter,
            category=category_filter,
        ),
    )
    await session.commit()
    return rows


@router.get("/deposits/settings", response_model=CourierDepositSettingsRead)
async def get_courier_deposit_settings(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, int | bool]:
    require_manager_plus(actor)
    return await deposit_service.get_deposit_settings(session)


@router.put("/deposits/settings", response_model=CourierDepositSettingsRead)
async def put_courier_deposit_settings(
    payload: CourierDepositSettingsUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, int | bool]:
    require_manager_plus(actor)
    values = payload.model_dump(exclude_unset=True, exclude_none=True)
    updated = await deposit_service.update_deposit_settings(session, values, actor.user_id)
    await session.commit()
    return updated


@router.get("/{employee_id}/deposit", response_model=CourierDepositCardRead)
async def get_courier_deposit_card(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    require_manager_plus(actor)
    account = await deposit_service.ensure_account(session, employee_id)
    transactions = await deposit_service.list_transactions(session, employee_id)
    balance = await deposit_service.get_balance(session, employee_id)
    await session.commit()
    return {
        "account": deposit_service.account_payload(account),
        "balance_cents": balance,
        "transactions": [
            deposit_service.transaction_payload(transaction) for transaction in transactions
        ],
    }


@router.put("/{employee_id}/deposit/opening", response_model=CourierDepositCardRead)
async def put_courier_deposit_opening(
    employee_id: uuid.UUID,
    payload: CourierDepositOpeningUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    require_manager_plus(actor)
    account = await deposit_service.set_opening_balance(
        session,
        employee_id=employee_id,
        amount_cents=payload.amount_cents,
        opening_date=payload.opening_date,
        actor_id=payload.actor_id,
    )
    transactions = await deposit_service.list_transactions(session, employee_id)
    balance = await deposit_service.get_balance(session, employee_id)
    await session.commit()
    await session.refresh(account)
    return {
        "account": deposit_service.account_payload(account),
        "balance_cents": balance,
        "transactions": [
            deposit_service.transaction_payload(transaction) for transaction in transactions
        ],
    }


@router.post("/{employee_id}/deposit/transactions", response_model=CourierDepositTransactionRead)
async def post_courier_deposit_transaction(
    employee_id: uuid.UUID,
    payload: CourierDepositTransactionCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    require_manager_plus(actor)
    transaction = await deposit_service.create_transaction(
        session,
        employee_id=employee_id,
        transaction_type=payload.transaction_type,
        amount_cents=payload.amount_cents,
        transaction_date=payload.transaction_date,
        comment=payload.comment,
        actor_id=payload.actor_id,
    )
    await session.commit()
    await session.refresh(transaction)
    return deposit_service.transaction_payload(transaction)


@router.get("/evaluation-criteria", response_model=list[CourierEvaluationCriterionRead])
async def get_courier_evaluation_criteria(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[CourierEvaluationCriterion]:
    require_manager_plus(actor)
    result = await session.scalars(
        select(CourierEvaluationCriterion)
        .where(CourierEvaluationCriterion.is_active.is_(True))
        .order_by(CourierEvaluationCriterion.display_order)
    )
    return list(result.all())


@router.get("/evaluations", response_model=list[CourierEvaluationRead])
async def get_courier_evaluations(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    courier: Annotated[uuid.UUID | None, Query()] = None,
    author: Annotated[uuid.UUID | None, Query()] = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    criterion: Annotated[int | None, Query()] = None,
) -> list[dict[str, Any]]:
    require_manager_plus(actor)
    if date_from is not None and date_to is not None:
        ensure_date_range(date_from, date_to)
    evaluations = await evaluation_service.list_evaluations(
        session,
        evaluation_service.EvaluationFilters(
            courier_id=courier,
            author_id=author,
            date_from=date_from,
            date_to=date_to,
            criterion_id=criterion,
        ),
    )
    return [evaluation_service.evaluation_payload(evaluation) for evaluation in evaluations]


@router.post("/evaluations", response_model=CourierEvaluationRead)
async def post_courier_evaluation(
    payload: CourierEvaluationCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    require_manager_plus(actor)
    evaluation = await evaluation_service.create_evaluation(
        session,
        courier_id=payload.courier_employee_id,
        criterion_id=payload.criterion_id,
        evaluated_at=payload.evaluated_at,
        comment=payload.comment,
        actor_id=payload.actor_id,
        source=payload.source,
    )
    await session.commit()
    await session.refresh(evaluation)
    return evaluation_service.evaluation_payload(evaluation)


@router.patch("/evaluations/{evaluation_id}", response_model=CourierEvaluationRead)
async def patch_courier_evaluation(
    evaluation_id: int,
    payload: CourierEvaluationUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    require_manager_plus(actor)
    changes = payload.model_dump(exclude_unset=True)
    actor_id = changes.pop("actor_id")
    evaluation = await evaluation_service.update_evaluation(
        session,
        evaluation_id=evaluation_id,
        actor_id=actor_id,
        **changes,
    )
    await session.commit()
    await session.refresh(evaluation)
    return evaluation_service.evaluation_payload(evaluation)


@router.delete("/evaluations/{evaluation_id}", response_model=CourierEvaluationRead)
async def delete_courier_evaluation(
    evaluation_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    actor_id: Annotated[uuid.UUID, Query()],
) -> dict[str, Any]:
    require_manager_plus(actor)
    evaluation = await evaluation_service.delete_evaluation(
        session,
        evaluation_id=evaluation_id,
        actor_id=actor_id,
    )
    await session.commit()
    await session.refresh(evaluation)
    return evaluation_service.evaluation_payload(evaluation)


@router.get(
    "/{employee_id}/evaluations/monthly",
    response_model=CourierEvaluationMonthlyAggregate,
)
async def get_courier_evaluation_monthly(
    employee_id: uuid.UUID,
    month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    require_manager_plus(actor)
    return await evaluation_service.monthly_aggregate(session, employee_id, _parse_month(month))


@router.get("/schedule", response_model=list[CourierScheduleEntryRead])
async def get_courier_schedule(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
    courier: Annotated[uuid.UUID | None, Query()] = None,
) -> list[dict[str, Any]]:
    require_manager_plus(actor)
    ensure_date_range(date_from, date_to)
    entries = await schedule_service.list_entries(
        session,
        schedule_service.ScheduleFilters(
            date_from=date_from,
            date_to=date_to,
            courier_id=courier,
        ),
    )
    return [schedule_service.entry_payload(entry) for entry in entries]


@router.put("/{employee_id}/schedule/{work_date}", response_model=CourierScheduleEntryRead)
async def put_courier_schedule_entry(
    employee_id: uuid.UUID,
    work_date: date,
    payload: CourierScheduleUpsert,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    require_manager_plus(actor)
    entry = await schedule_service.upsert_entry(
        session,
        courier_id=employee_id,
        work_date=work_date,
        planned_start_at=payload.planned_start_at,
        planned_end_at=payload.planned_end_at,
        comment=payload.comment,
        actor_id=payload.actor_id,
    )
    await session.commit()
    await session.refresh(entry)
    return schedule_service.entry_payload(entry)


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


def _category_assignment_payload(assignment: Any) -> dict[str, Any]:
    return {
        "id": assignment.id,
        "employee_id": assignment.employee_id,
        "category": _enum_value(assignment.category),
        "effective_from": assignment.effective_from,
        "effective_to": assignment.effective_to,
        "created_by": assignment.created_by,
        "created_at": assignment.created_at,
    }


def _parse_month(value: str) -> date:
    try:
        year, month = value.split("-", 1)
        return date(int(year), int(month), 1)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="month must be in YYYY-MM format",
        ) from exc


def _enum_value(value: Any) -> str | None:
    if value is None:
        return None
    return getattr(value, "value", value)

from __future__ import annotations

import http.client as _http_client
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentActor,
    ensure_permission,
    get_current_actor,
    require_any_permission,
    require_permission,
)
from app.auth.permissions import payout_channel_permission
from app.db.session import get_session
from app.models import (
    CourierDepositTransactionType,
    CourierEvaluation,
    CourierEvaluationCriterion,
    CourierIikoShift,
    CourierShiftMatch,
    DeliveryOrder,
    Employee,
    User,
)
from app.schemas.couriers import (
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
    CourierListResponse,
    CourierScheduleEntryRead,
    CourierScheduleMatchedEntry,
    CourierScheduleUpsert,
    CourierShiftDayResponse,
    CourierShiftReviewUpdate,
    CourierStatisticsDetail,
    CourierStatisticsRow,
    CourierSubstitutionRequest,
    CourierWorkStatusFilter,
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
    deposit_service,
    evaluation_service,
    iiko_attendance_sync,
    kpi_service,
    schedule_service,
    shift_day_service,
)
from app.services.couriers.deposit_iiko_payout import post_deposit_return_to_iiko
from app.services.couriers.iiko_olap_sync import sync_courier_olap_deliveries
from app.services.deposit_bank_draft import send_deposit_payout_bank_draft
from app.services.position_registry import courier_positions

router = APIRouter()
COURIERS_LIST_READ_ACCESS = (Depends(require_permission("couriers.list.read")),)
COURIERS_STATISTICS_READ_ACCESS = (Depends(require_permission("couriers.statistics.read")),)
COURIERS_SCHEDULE_READ_ACCESS = (Depends(require_permission("couriers.schedule.read")),)
COURIERS_SCHEDULE_EDIT_ACCESS = (Depends(require_permission("couriers.schedule.edit")),)
COURIERS_SHIFTS_SYNC_ACCESS = (Depends(require_permission("couriers.shifts.sync")),)
COURIERS_EVALUATIONS_READ_ACCESS = (Depends(require_permission("couriers.evaluations.read")),)
COURIERS_EVALUATIONS_EDIT_ACCESS = (Depends(require_permission("couriers.evaluations.edit")),)
COURIERS_DEPOSITS_READ_ACCESS = (Depends(require_permission("couriers.deposits.read")),)
# Операции с курьерским депозитом разведены на пополнение/возврат/списание — проверяются
# динамически по типу операции внутри post_courier_deposit_transaction. Настройка (начальный
# баланс) — отдельное право couriers.deposits.configure.
COURIERS_DEPOSITS_CONFIGURE_ACCESS = (
    Depends(require_permission("couriers.deposits.configure")),
)
COURIERS_DEPOSITS_OP_PERMISSIONS = {
    "top_up": "couriers.deposits.top_up",
    "return": "couriers.deposits.return",
    "forfeit": "couriers.deposits.forfeit",
}
COURIERS_DEPOSITS_SETTINGS_READ_ACCESS = (
    Depends(
        require_any_permission(("couriers.deposits.configure", "source.deposit_settings.read"))
    ),
)
COURIERS_DEPOSITS_SETTINGS_EDIT_ACCESS = (
    Depends(
        require_any_permission(("couriers.deposits.configure", "source.deposit_settings.edit"))
    ),
)
COURIERS_SHIFT_DAY_READ_ACCESS = (
    Depends(
        require_any_permission(("couriers.deposits.read", "couriers.evaluations.read"))
    ),
)
COURIERS_SHIFT_DAY_EDIT_ACCESS = (
    Depends(
        require_any_permission(
            (
                "couriers.deposits.top_up",
                "couriers.deposits.return",
                "couriers.deposits.forfeit",
                "couriers.evaluations.edit",
            )
        )
    ),
)
CourierSyncMode = Literal["hot", "cold", "custom"]
MAX_DELIVERY_WINDOW_DAYS = 92


class CourierSyncRequest(BaseModel):
    mode: CourierSyncMode = "hot"
    date_from: date | None = None
    date_to: date | None = None


@router.post("/sync", dependencies=COURIERS_SHIFTS_SYNC_ACCESS)
async def post_courier_sync(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    payload: Annotated[CourierSyncRequest | None, Body()] = None,
    mode: Annotated[CourierSyncMode | None, Query()] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
) -> dict[str, int]:
    resolved_mode = mode or (payload.mode if payload is not None else "hot")
    resolved_date_from = (
        date_from if date_from is not None else (payload.date_from if payload is not None else None)
    )
    resolved_date_to = (
        date_to if date_to is not None else (payload.date_to if payload is not None else None)
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


@router.post("/iiko/sync-attendance", dependencies=COURIERS_SHIFTS_SYNC_ACCESS)
async def post_iiko_attendance_sync(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
) -> dict[str, Any]:
    ensure_date_range(date_from, date_to)
    ensure_window(date_from, date_to, max_days=MAX_DELIVERY_WINDOW_DAYS)
    try:
        report = await iiko_attendance_sync.sync_attendance(
            session,
            from_date=date_from,
            to_date=date_to,
            run_reason="manual",
        )
    except _http_client.IncompleteRead as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="iiko не отвечает — сервер оборвал соединение. Попробуйте через минуту.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return report.as_dict()


@router.get("/deliveries", dependencies=COURIERS_STATISTICS_READ_ACCESS)
async def get_courier_deliveries(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    date_from: Annotated[date, Query()],
    date_to: Annotated[date, Query()],
    courier_iiko_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(gt=0, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
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


@router.get("/shifts", dependencies=COURIERS_STATISTICS_READ_ACCESS)
async def get_courier_shifts(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    date_from: Annotated[date, Query()],
    date_to: Annotated[date, Query()],
    employee_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[dict[str, Any]]:
    ensure_date_range(date_from, date_to)
    ensure_window(date_from, date_to, max_days=MAX_DELIVERY_WINDOW_DAYS)
    return await list_courier_shifts(
        session,
        date_from=date_from,
        date_to=date_to,
        employee_id=employee_id,
    )


@router.get("/iiko-shifts", dependencies=COURIERS_STATISTICS_READ_ACCESS)
async def get_iiko_courier_shifts(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
    employee_id: Annotated[uuid.UUID | None, Query()] = None,
    is_open: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(gt=0, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Реестр смен курьеров из courier_iiko_shift с резолвом ФИО."""
    ensure_date_range(date_from, date_to)
    ensure_window(date_from, date_to, max_days=MAX_DELIVERY_WINDOW_DAYS)

    start_at = datetime.combine(date_from, datetime.min.time(), tzinfo=MOSCOW_TZ)
    end_at = datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=MOSCOW_TZ)
    query = (
        select(CourierIikoShift, Employee, CourierShiftMatch)
        .outerjoin(Employee, Employee.id == CourierIikoShift.employee_id)
        .outerjoin(
            CourierShiftMatch,
            CourierShiftMatch.iiko_shift_id == CourierIikoShift.id,
        )
        .where(
            CourierIikoShift.opened_at >= start_at,
            CourierIikoShift.opened_at < end_at,
        )
    )
    if employee_id is not None:
        query = query.where(CourierIikoShift.employee_id == employee_id)
    if is_open is True:
        query = query.where(CourierIikoShift.closed_at.is_(None))
    if is_open is False:
        query = query.where(CourierIikoShift.closed_at.is_not(None))
    query = query.order_by(CourierIikoShift.opened_at.desc()).limit(limit).offset(offset)
    rows = (await session.execute(query)).all()

    items: list[dict[str, Any]] = []
    for shift, employee, match in rows:
        worked_minutes: int | None = None
        if shift.closed_at is not None:
            worked_minutes = int((shift.closed_at - shift.opened_at).total_seconds() // 60)
        items.append(
            {
                "id": shift.id,
                "iiko_employee_id": shift.iiko_employee_id,
                "employee_id": str(shift.employee_id) if shift.employee_id else None,
                "employee_name": employee.full_name if employee is not None else None,
                "opened_at": shift.opened_at.isoformat(),
                "closed_at": shift.closed_at.isoformat() if shift.closed_at else None,
                "worked_minutes": worked_minutes,
                "is_open": shift.closed_at is None,
                "attendance_type": shift.attendance_type,
                "match_status": match.status.value if match is not None else None,
                "match_category": (
                    "primary"
                    if match is not None and match.status.value.endswith("_primary")
                    else "secondary"
                    if match is not None and match.status.value.endswith("_secondary")
                    else None
                ),
            }
        )
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/iiko-deliveries", dependencies=COURIERS_STATISTICS_READ_ACCESS)
async def get_iiko_courier_deliveries(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
    employee_id: Annotated[uuid.UUID | None, Query()] = None,
    service_type: Annotated[str | None, Query()] = None,
    include_cancelled: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(gt=0, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Реестр доставок с резолвом ФИО курьера и сводкой за период."""
    ensure_date_range(date_from, date_to)
    ensure_window(date_from, date_to, max_days=MAX_DELIVERY_WINDOW_DAYS)

    base = select(DeliveryOrder).where(
        DeliveryOrder.work_date >= date_from,
        DeliveryOrder.work_date <= date_to,
    )
    if employee_id is not None:
        employee = await session.get(Employee, employee_id)
        if employee is None or not employee.iiko_id:
            return {
                "items": [],
                "summary": {"count": 0, "avg_minutes": None, "revenue_total": "0"},
                "limit": limit,
                "offset": offset,
            }
        base = base.where(DeliveryOrder.courier_iiko_id == employee.iiko_id)
    if service_type:
        base = base.where(DeliveryOrder.service_type == service_type)
    if not include_cancelled:
        from sqlalchemy import func, or_

        from app.services.couriers.shift_matching import NON_DELIVERY_STATUSES as _CANCELLED

        status_lower = func.lower(DeliveryOrder.status)
        base = base.where(
            or_(
                DeliveryOrder.status.is_(None),
                status_lower.notin_(_CANCELLED),
            )
        )

    list_query = (
        base.order_by(
            DeliveryOrder.taken_at.desc().nullslast(),
            DeliveryOrder.opened_at.desc().nullslast(),
        )
        .limit(limit)
        .offset(offset)
    )
    orders = (await session.scalars(list_query)).all()

    courier_iiko_ids = {o.courier_iiko_id for o in orders if o.courier_iiko_id}
    employees: dict[str, str] = {}
    if courier_iiko_ids:
        emp_rows = (
            await session.scalars(
                select(Employee).where(Employee.iiko_id.in_(courier_iiko_ids))
            )
        ).all()
        for emp in emp_rows:
            if emp.iiko_id:
                employees[emp.iiko_id] = emp.full_name

    items: list[dict[str, Any]] = []
    for order in orders:
        items.append(
            {
                "id": str(order.id),
                "iiko_order_id": order.iiko_order_id,
                "order_number": order.order_number,
                "work_date": order.work_date.isoformat(),
                "status": order.status,
                "service_type": order.service_type,
                "courier_iiko_id": order.courier_iiko_id,
                "courier_employee_name": (
                    employees.get(order.courier_iiko_id) if order.courier_iiko_id else None
                ),
                "opened_at": order.opened_at.isoformat() if order.opened_at else None,
                "taken_at": order.taken_at.isoformat() if order.taken_at else None,
                "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
                "way_duration_minutes": (
                    str(order.way_duration_minutes)
                    if order.way_duration_minutes is not None
                    else None
                ),
                "revenue": str(order.revenue) if order.revenue is not None else None,
            }
        )

    from sqlalchemy import func as _func

    summary_row = (
        await session.execute(
            select(
                _func.count(DeliveryOrder.id),
                _func.avg(DeliveryOrder.way_duration_minutes),
                _func.coalesce(_func.sum(DeliveryOrder.revenue), 0),
            ).select_from(base.subquery())
        )
    ).one()
    count_total, avg_minutes, revenue_total = summary_row

    return {
        "items": items,
        "summary": {
            "count": int(count_total or 0),
            "avg_minutes": float(avg_minutes) if avg_minutes is not None else None,
            "revenue_total": str(revenue_total) if revenue_total is not None else "0",
        },
        "limit": limit,
        "offset": offset,
    }


@router.post("/iiko/sync-deliveries", dependencies=COURIERS_SHIFTS_SYNC_ACCESS)
async def post_iiko_delivery_sync(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
) -> dict[str, int]:
    """Manual sync доставок через OLAP. Без параметров — hot window (вчера-сегодня)."""
    today = datetime.now(MOSCOW_TZ).date()
    if date_from is None or date_to is None:
        date_from = today - timedelta(days=1)
        date_to = today

    if date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date_from must be <= date_to",
        )
    # Cap to today, чтобы не уйти в будущее
    if date_to > today:
        date_to = today
    if date_from > today:
        date_from = today

    try:
        result = await sync_courier_olap_deliveries(
            session, date_from=date_from, date_to=date_to
        )
        await session.commit()
    except RuntimeError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    return result.as_dict()


@router.get(
    "/statistics",
    response_model=list[CourierStatisticsRow],
    dependencies=COURIERS_STATISTICS_READ_ACCESS,
)
async def get_couriers_statistics(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")],
) -> list[dict[str, Any]]:
    month_start = _parse_month(month)
    kpis = await kpi_service.list_couriers_kpi(session, month_start)
    rows: list[dict[str, Any]] = []
    for kpi in kpis:
        rows.append(
            {
                "kpi": kpi,
                "evaluations": await evaluation_service.get_evaluations_monthly_summary(
                    session,
                    kpi.courier_id,
                    month_start,
                ),
            }
        )
    return rows


@router.get("/list", response_model=CourierListResponse, dependencies=COURIERS_LIST_READ_ACCESS)
async def get_couriers_list(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    status_filter: Annotated[CourierDepositStatus, Query(alias="status")] = "active",
    work_status_filter: Annotated[CourierWorkStatusFilter, Query(alias="work_status")] = "all",
    month: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
) -> dict[str, Any]:
    month_start = _parse_month(month) if month else _current_month_start()
    month_end = kpi_service.month_bounds(month_start)[1]
    couriers = (
        await session.scalars(
            select(Employee).where(Employee.position.in_(courier_positions())).order_by(Employee.full_name)
        )
    ).all()
    active_couriers = [courier for courier in couriers if courier.status == "active"]
    work_statuses = await schedule_service.get_work_statuses(
        session,
        [courier.id for courier in active_couriers],
    )
    rows = []
    open_shift_now_total = 0
    for courier in couriers:
        work_status = work_statuses.get(courier.id) if courier.status == "active" else None
        if not _matches_status_filter(courier, status_filter):
            continue
        if not _matches_work_status_filter(courier, work_status, work_status_filter):
            continue
        open_shift_now = await kpi_service.has_open_shift_now(session, courier.id)
        if courier.status == "active" and open_shift_now:
            open_shift_now_total += 1
        primary_count, secondary_count = await kpi_service.month_schedule_counts(
            session,
            courier.id,
            month_start,
        )
        rows.append(
            {
                "employee_id": courier.id,
                "full_name": courier.full_name,
                "iiko_id": courier.iiko_id,
                "is_courier_placeholder": courier.is_courier_placeholder,
                "status": courier.status,
                "work_status": work_status,
                "open_shift_now": open_shift_now,
                "primary_shifts_in_month": primary_count,
                "secondary_shifts_in_month": secondary_count,
            }
        )

    return {
        "month": month_start.strftime("%Y-%m"),
        "summary": {
            "active_total": len(active_couriers),
            "fired_this_month": sum(
                1
                for courier in couriers
                if courier.status != "active"
                and courier.fire_date is not None
                and month_start <= courier.fire_date < month_end
            ),
            "open_shift_now_total": open_shift_now_total,
            "reserve_total": sum(
                1 for courier in active_couriers if work_statuses.get(courier.id) == "reserve"
            ),
        },
        "rows": rows,
    }


@router.get(
    "/{employee_id}/statistics",
    response_model=CourierStatisticsDetail,
    dependencies=COURIERS_STATISTICS_READ_ACCESS,
)
async def get_courier_statistics_detail(
    employee_id: uuid.UUID,
    month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    month_start = _parse_month(month)
    courier = await session.get(Employee, employee_id)
    return {
        "courier_iiko_id": courier.iiko_id if courier is not None else "",
        "kpi": await kpi_service.get_courier_kpi(session, employee_id, month_start),
        "evaluations": await evaluation_service.get_evaluations_monthly_summary(
            session,
            employee_id,
            month_start,
        ),
        "latest_evaluations": await _latest_evaluation_payloads(
            session,
            employee_id,
            month_start,
            limit=20,
        ),
        "last_shift": await kpi_service.get_last_shift(session, employee_id, month_start),
        "discipline_breakdown": await kpi_service.get_discipline_breakdown(
            session,
            employee_id,
            month_start,
        ),
    }


@router.get(
    "/deposits",
    response_model=list[CourierDepositRow],
    dependencies=COURIERS_DEPOSITS_READ_ACCESS,
)
async def get_courier_deposits(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    status_filter: Annotated[CourierDepositStatus, Query(alias="status")] = "active",
    category_filter: Annotated[CourierDepositCategory, Query(alias="category")] = "all",
) -> list[dict[str, Any]]:
    rows = await deposit_service.list_couriers_with_balances(
        session,
        deposit_service.CourierDepositFilters(
            status=status_filter,
            category=category_filter,
        ),
    )
    await session.commit()
    return rows


@router.get(
    "/deposits/settings",
    response_model=CourierDepositSettingsRead,
    dependencies=COURIERS_DEPOSITS_SETTINGS_READ_ACCESS,
)
async def get_courier_deposit_settings(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, int | bool]:
    return await deposit_service.get_deposit_settings(session)


@router.put(
    "/deposits/settings",
    response_model=CourierDepositSettingsRead,
    dependencies=COURIERS_DEPOSITS_SETTINGS_EDIT_ACCESS,
)
async def put_courier_deposit_settings(
    payload: CourierDepositSettingsUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, int | bool]:
    values = payload.model_dump(exclude_unset=True, exclude_none=True)
    updated = await deposit_service.update_deposit_settings(session, values, actor.user_id)
    await session.commit()
    return updated


@router.get(
    "/{employee_id}/deposit",
    response_model=CourierDepositCardRead,
    dependencies=COURIERS_DEPOSITS_READ_ACCESS,
)
async def get_courier_deposit_card(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    account = await deposit_service.ensure_account(session, employee_id)
    transactions = await deposit_service.list_transactions(session, employee_id)
    balance = await deposit_service.get_balance(session, employee_id)
    await session.commit()
    return {
        "account": deposit_service.account_payload(account),
        "balance_cents": balance,
        "transactions": await _deposit_transaction_payloads(session, transactions),
    }


@router.put(
    "/{employee_id}/deposit/opening",
    response_model=CourierDepositCardRead,
    dependencies=COURIERS_DEPOSITS_CONFIGURE_ACCESS,
)
async def put_courier_deposit_opening(
    employee_id: uuid.UUID,
    payload: CourierDepositOpeningUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    account = await deposit_service.set_opening_balance(
        session,
        employee_id=employee_id,
        amount_cents=payload.amount_cents,
        opening_date=payload.opening_date,
    )
    transactions = await deposit_service.list_transactions(session, employee_id)
    balance = await deposit_service.get_balance(session, employee_id)
    await session.commit()
    await session.refresh(account)
    return {
        "account": deposit_service.account_payload(account),
        "balance_cents": balance,
        "transactions": await _deposit_transaction_payloads(session, transactions),
    }


@router.post(
    "/{employee_id}/deposit/transactions",
    response_model=CourierDepositTransactionRead,
)
async def post_courier_deposit_transaction(
    employee_id: uuid.UUID,
    payload: CourierDepositTransactionCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    actor_user_id = _require_actor_user_id(actor)
    # Право на конкретную операцию (пополнение/возврат/списание).
    tt = payload.transaction_type
    tt_value = tt.value if isinstance(tt, CourierDepositTransactionType) else str(tt)
    op_permission = COURIERS_DEPOSITS_OP_PERMISSIONS.get(tt_value)
    if op_permission is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Неизвестный тип операции с депозитом",
        )
    ensure_permission(actor, op_permission)
    # Возврат с конкретного счёта требует ещё и права на канал (Сейф/ТК Черникова/банк-черновик).
    if tt_value == "return":
        channel_perm = payout_channel_permission(payload.payout_method or "cash_tk")
        if channel_perm is not None:
            ensure_permission(actor, channel_perm)
    transaction = await deposit_service.create_transaction(
        session,
        employee_id=employee_id,
        transaction_type=payload.transaction_type,
        amount_cents=payload.amount_cents,
        transaction_date=payload.transaction_date,
        comment=payload.comment,
        created_by_user_id=actor_user_id,
        payout_method=payload.payout_method,
    )
    await session.commit()
    await session.refresh(transaction)
    # Денежная синхронизация iiko «Главной кассы» (= ТК Черникова) — после commit (БД —
    # источник истины, ошибка iiko не откатывает операцию). FORFEIT кассу не двигает.
    # ПОПОЛНЕНИЕ (top_up) в iiko НЕ проводим: этот Resto API не умеет внесение (addPayIn=404,
    # addPayOut отвергает PAYIN-тип 409). Рост Главной кассы по пополнениям — вручную в
    # бэк-офисе iiko либо отдельным методом (исследуется). ДДС-приход книжится независимо.
    if tt_value == "return" and transaction.payout_method in (None, "cash_tk"):
        # Возврат наличными с ТК Черникова — изъятие из Главной кассы. Для Сейфа iiko не
        # трогаем (другой счёт), иначе ложное изъятие.
        post_deposit_return_to_iiko(transaction)
    elif tt_value == "return" and transaction.payout_method == "bank_draft":
        # Безналичный возврат «как ЗП»: черновик на ИП Шокину (раздача с Сейфа). После
        # commit, ошибка банка не валит возврат. iiko не трогаем (деньги не через кассу).
        await send_deposit_payout_bank_draft(
            session,
            document_id=f"teplo-courier-deposit-{transaction.id}",
            amount=(Decimal(transaction.amount_cents) / Decimal("100")).quantize(Decimal("0.01")),
            purpose=f"Возврат депозита курьеру (операция #{transaction.id}) через Сейф",
        )
    return await _deposit_transaction_payload(session, transaction)


@router.get(
    "/evaluation-criteria",
    response_model=list[CourierEvaluationCriterionRead],
    dependencies=COURIERS_EVALUATIONS_READ_ACCESS,
)
async def get_courier_evaluation_criteria(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[CourierEvaluationCriterion]:
    result = await session.scalars(
        select(CourierEvaluationCriterion)
        .where(CourierEvaluationCriterion.is_active.is_(True))
        .order_by(CourierEvaluationCriterion.display_order)
    )
    return list(result.all())


@router.get(
    "/evaluations",
    response_model=list[CourierEvaluationRead],
    dependencies=COURIERS_EVALUATIONS_READ_ACCESS,
)
async def get_courier_evaluations(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    courier: Annotated[uuid.UUID | None, Query()] = None,
    author: Annotated[uuid.UUID | None, Query()] = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    criterion: Annotated[int | None, Query()] = None,
) -> list[dict[str, Any]]:
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
    payloads = [evaluation_service.evaluation_payload(evaluation) for evaluation in evaluations]
    return await _attach_evaluation_author_names(session, payloads)


@router.post(
    "/evaluations",
    response_model=CourierEvaluationRead,
    dependencies=COURIERS_EVALUATIONS_EDIT_ACCESS,
)
async def post_courier_evaluation(
    payload: CourierEvaluationCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    actor_user_id = _require_actor_user_id(actor)
    evaluation = await evaluation_service.create_evaluation(
        session,
        courier_id=payload.courier_employee_id,
        criterion_id=payload.criterion_id,
        evaluated_at=payload.evaluated_at,
        comment=payload.comment,
        actor_id=actor_user_id,
        source=payload.source,
    )
    await session.commit()
    await session.refresh(evaluation)
    return evaluation_service.evaluation_payload(evaluation)


@router.patch(
    "/evaluations/{evaluation_id}",
    response_model=CourierEvaluationRead,
    dependencies=COURIERS_EVALUATIONS_EDIT_ACCESS,
)
async def patch_courier_evaluation(
    evaluation_id: int,
    payload: CourierEvaluationUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    changes = payload.model_dump(exclude_unset=True)
    changes.pop("actor_id", None)
    actor_user_id = _require_actor_user_id(actor)
    evaluation = await evaluation_service.update_evaluation(
        session,
        evaluation_id=evaluation_id,
        actor_id=actor_user_id,
        **changes,
    )
    await session.commit()
    await session.refresh(evaluation)
    return evaluation_service.evaluation_payload(evaluation)


@router.delete(
    "/evaluations/{evaluation_id}",
    response_model=CourierEvaluationRead,
    dependencies=COURIERS_EVALUATIONS_EDIT_ACCESS,
)
async def delete_courier_evaluation(
    evaluation_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    actor_user_id = _require_actor_user_id(actor)
    evaluation = await evaluation_service.delete_evaluation(
        session,
        evaluation_id=evaluation_id,
        actor_id=actor_user_id,
    )
    await session.commit()
    await session.refresh(evaluation)
    return evaluation_service.evaluation_payload(evaluation)


@router.get(
    "/{employee_id}/evaluations/monthly",
    response_model=CourierEvaluationMonthlyAggregate,
    dependencies=COURIERS_EVALUATIONS_READ_ACCESS,
)
async def get_courier_evaluation_monthly(
    employee_id: uuid.UUID,
    month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    return await evaluation_service.monthly_aggregate(session, employee_id, _parse_month(month))


@router.get(
    "/shift-day",
    response_model=CourierShiftDayResponse,
    dependencies=COURIERS_SHIFT_DAY_READ_ACCESS,
)
async def get_courier_shift_day(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    work_date: Annotated[date, Query(alias="date")],
) -> dict[str, Any]:
    return await shift_day_service.get_shift_day(session, work_date)


@router.put(
    "/shift-day/{work_date}/courier/{employee_id}/review",
    response_model=CourierShiftDayResponse,
    dependencies=COURIERS_SHIFT_DAY_EDIT_ACCESS,
)
async def put_courier_shift_review(
    work_date: date,
    employee_id: uuid.UUID,
    payload: CourierShiftReviewUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    actor_user_id = _require_actor_user_id(actor)
    changes = payload.model_dump(exclude_unset=True)
    await shift_day_service.set_review(
        session,
        work_date=work_date,
        courier_employee_id=employee_id,
        actor_id=actor_user_id,
        eval_skipped=changes.get("eval_skipped"),
        deposit_skipped=changes.get("deposit_skipped"),
        deposit_skip_comment=changes.get("deposit_skip_comment", shift_day_service.UNSET),
    )
    snapshot = await shift_day_service.get_shift_day(session, work_date)
    await session.commit()
    return snapshot


@router.put(
    "/shift-day/{work_date}/courier/{placeholder_id}/substitute",
    response_model=CourierShiftDayResponse,
    dependencies=COURIERS_SHIFT_DAY_EDIT_ACCESS,
)
async def put_courier_substitution(
    work_date: date,
    placeholder_id: uuid.UUID,
    payload: CourierSubstitutionRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    actor_user_id = _require_actor_user_id(actor)
    await shift_day_service.set_substitution(
        session,
        work_date=work_date,
        placeholder_employee_id=placeholder_id,
        real_employee_id=payload.real_employee_id,
        actor_id=actor_user_id,
    )
    snapshot = await shift_day_service.get_shift_day(session, work_date)
    await session.commit()
    return snapshot


@router.delete(
    "/shift-day/{work_date}/courier/{placeholder_id}/substitute",
    response_model=CourierShiftDayResponse,
    dependencies=COURIERS_SHIFT_DAY_EDIT_ACCESS,
)
async def delete_courier_substitution(
    work_date: date,
    placeholder_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    await shift_day_service.clear_substitution(
        session,
        work_date=work_date,
        placeholder_employee_id=placeholder_id,
    )
    snapshot = await shift_day_service.get_shift_day(session, work_date)
    await session.commit()
    return snapshot


@router.post(
    "/shift-day/{work_date}/confirm",
    response_model=CourierShiftDayResponse,
    dependencies=COURIERS_SHIFT_DAY_EDIT_ACCESS,
)
async def post_courier_shift_day_confirm(
    work_date: date,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    actor_user_id = _require_actor_user_id(actor)
    snapshot = await shift_day_service.confirm_day(session, work_date, actor_user_id)
    await session.commit()
    return snapshot


@router.post(
    "/shift-day/{work_date}/unconfirm",
    response_model=CourierShiftDayResponse,
    dependencies=COURIERS_SHIFT_DAY_EDIT_ACCESS,
)
async def post_courier_shift_day_unconfirm(
    work_date: date,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    snapshot = await shift_day_service.unconfirm_day(session, work_date)
    await session.commit()
    return snapshot


@router.get(
    "/schedule",
    response_model=list[CourierScheduleEntryRead],
    dependencies=COURIERS_SCHEDULE_READ_ACCESS,
)
async def get_courier_schedule(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
    courier: Annotated[uuid.UUID | None, Query()] = None,
) -> list[dict[str, Any]]:
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


@router.get(
    "/schedule/matched",
    response_model=list[CourierScheduleMatchedEntry],
    dependencies=COURIERS_SCHEDULE_READ_ACCESS,
)
async def get_courier_schedule_matched(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
    courier: Annotated[uuid.UUID | None, Query()] = None,
) -> list[dict[str, Any]]:
    ensure_date_range(date_from, date_to)
    return await schedule_service.list_matched_entries(
        session,
        schedule_service.ScheduleFilters(
            date_from=date_from,
            date_to=date_to,
            courier_id=courier,
        ),
    )


@router.put(
    "/{employee_id}/schedule/{work_date}",
    response_model=CourierScheduleEntryRead,
    dependencies=COURIERS_SCHEDULE_EDIT_ACCESS,
)
async def put_courier_schedule_entry(
    employee_id: uuid.UUID,
    work_date: date,
    payload: CourierScheduleUpsert,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    actor_user_id = _require_actor_user_id(actor)
    entry = await schedule_service.upsert_entry(
        session,
        courier_id=employee_id,
        work_date=work_date,
        category=payload.category,
        planned_start_at=payload.planned_start_at,
        planned_end_at=payload.planned_end_at,
        comment=payload.comment,
        actor_id=actor_user_id,
    )
    await session.commit()
    await session.refresh(entry)
    return schedule_service.entry_payload(entry)


@router.delete(
    "/{employee_id}/schedule/{work_date}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=COURIERS_SCHEDULE_EDIT_ACCESS,
)
async def delete_courier_schedule_entry(
    employee_id: uuid.UUID,
    work_date: date,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> None:
    actor_user_id = _require_actor_user_id(actor)
    await schedule_service.delete_entry(
        session,
        courier_id=employee_id,
        work_date=work_date,
        actor_id=actor_user_id,
    )
    await session.commit()


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


async def _deposit_transaction_payload(
    session: AsyncSession,
    transaction: Any,
) -> dict[str, Any]:
    created_by_name = await _user_name(session, transaction.created_by)
    return deposit_service.transaction_payload(transaction, created_by_name)


async def _deposit_transaction_payloads(
    session: AsyncSession,
    transactions: list[Any],
) -> list[dict[str, Any]]:
    user_ids = {transaction.created_by for transaction in transactions}
    if not user_ids:
        return []
    users = (await session.scalars(select(User).where(User.id.in_(user_ids)))).all()
    names = {user.id: user.full_name for user in users}
    return [
        deposit_service.transaction_payload(transaction, names.get(transaction.created_by))
        for transaction in transactions
    ]


async def _user_name(session: AsyncSession, user_id: uuid.UUID) -> str | None:
    user = await session.get(User, user_id)
    return user.full_name if user is not None else None


async def _attach_evaluation_author_names(
    session: AsyncSession,
    payloads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    user_ids = {payload["created_by"] for payload in payloads if payload.get("created_by")}
    names: dict[uuid.UUID, str] = {}
    if user_ids:
        users = (await session.scalars(select(User).where(User.id.in_(user_ids)))).all()
        names = {user.id: user.full_name for user in users}
    for payload in payloads:
        payload["author_name"] = names.get(payload.get("created_by"))
    return payloads


def _parse_month(value: str) -> date:
    try:
        year, month = value.split("-", 1)
        return date(int(year), int(month), 1)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="month must be in YYYY-MM format",
        ) from exc


def _current_month_start() -> date:
    return datetime.now(MOSCOW_TZ).date().replace(day=1)


def _matches_status_filter(employee: Employee, status_filter: str) -> bool:
    if status_filter == "all":
        return True
    if status_filter == "active":
        return employee.status == "active"
    return employee.status != "active"


def _matches_work_status_filter(
    employee: Employee,
    work_status: str | None,
    work_status_filter: str,
) -> bool:
    if work_status_filter == "all":
        return True
    return employee.status == "active" and work_status == work_status_filter


async def _latest_evaluation_payloads(
    session: AsyncSession,
    courier_id: uuid.UUID,
    month: date,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    month_start, month_end = kpi_service.month_bounds(month)
    result = await session.execute(
        select(CourierEvaluation, CourierEvaluationCriterion, User)
        .join(
            CourierEvaluationCriterion,
            CourierEvaluationCriterion.id == CourierEvaluation.criterion_id,
        )
        .outerjoin(User, User.id == CourierEvaluation.created_by)
        .where(
            CourierEvaluation.courier_employee_id == courier_id,
            CourierEvaluation.deleted_at.is_(None),
            CourierEvaluation.evaluated_at >= month_start,
            CourierEvaluation.evaluated_at < month_end,
        )
        .order_by(CourierEvaluation.evaluated_at.desc(), CourierEvaluation.created_at.desc())
        .limit(limit)
    )
    payloads: list[dict[str, Any]] = []
    for evaluation, criterion, author in result.all():
        payload = evaluation_service.evaluation_payload(evaluation)
        payload.update(
            {
                "criterion_code": criterion.code,
                "criterion_label": criterion.label,
                "author_name": author.full_name if author is not None else None,
            }
        )
        payloads.append(payload)
    return payloads


def _require_actor_user_id(actor: CurrentActor) -> uuid.UUID:
    if actor.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Не удалось определить текущего пользователя",
        )
    return actor.user_id



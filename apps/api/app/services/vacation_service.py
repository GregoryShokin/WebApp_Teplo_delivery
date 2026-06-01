from __future__ import annotations

import calendar
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor
from app.models import (
    AgentAction,
    AgentRun,
    AppSetting,
    Employee,
    ScheduledShift,
    ShiftSchedule,
    VacationPeriod,
)

VACATION_EMPLOYEE_POSITIONS = frozenset({"Повар", "Кассир"})
ACTIVE_VACATION_STATUSES = frozenset({"planned", "paid"})
DEFAULT_DAYS_PER_YEAR_LIMIT = 20
DEFAULT_DAILY_AMOUNT = Decimal("1000")
MAX_DAYS_PER_PERIOD = 10
MIN_MONTHS_BETWEEN_PERIODS = 2
VACATION_DAYS_LIMIT_KEY = "vacation.days_per_year_limit"
VACATION_DAILY_AMOUNT_KEY = "vacation.daily_amount"

UNSET = object()


@dataclass(frozen=True, slots=True)
class ShiftConflict:
    shift_id: uuid.UUID
    business_date: date
    schedule_id: uuid.UUID
    schedule_status: str


@dataclass(frozen=True, slots=True)
class VacationBalance:
    employee_id: uuid.UUID
    year: int
    limit: int
    used: int
    remaining: int
    periods: list[VacationPeriod]


class VacationShiftConflictError(RuntimeError):
    def __init__(self, conflicts: list[ShiftConflict]) -> None:
        super().__init__("На период отпуска уже есть смены")
        self.conflicts = conflicts


async def create_vacation_period(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    date_start: date,
    date_end: date,
    comment: str | None,
    actor: CurrentActor,
    force_remove_conflicting_shifts: bool = False,
) -> VacationPeriod:
    employee = await _get_vacation_employee(session, employee_id)
    days_count = await _validate_period_payload(
        session,
        employee=employee,
        date_start=date_start,
        date_end=date_end,
        exclude_period_id=None,
    )
    before: dict[str, Any] | None = None
    period = VacationPeriod(
        id=uuid.uuid4(),
        employee_id=employee.id,
        date_start=date_start,
        date_end=date_end,
        days_count=days_count,
        status="planned",
        comment=_clean_comment(comment),
        created_by_user_id=_actor_user_id(actor),
    )
    conflicts = await _load_shift_conflicts(
        session,
        employee_id=employee.id,
        date_start=date_start,
        date_end=date_end,
    )
    await _handle_shift_conflicts(
        session,
        conflicts,
        force_remove_conflicting_shifts=force_remove_conflicting_shifts,
    )
    session.add(period)
    await session.flush()
    await _add_audit_action(
        session,
        action_type="vacation_period_create",
        target_id=period.id,
        before=before,
        after=_period_snapshot(period),
        actor=actor,
    )
    await _commit_refresh(session, period)
    return period


async def update_vacation_period(
    session: AsyncSession,
    period_id: uuid.UUID,
    *,
    date_start: date | None = None,
    date_end: date | None = None,
    comment: str | None | object = UNSET,
    actor: CurrentActor,
    force_remove_conflicting_shifts: bool = False,
) -> VacationPeriod:
    period = await session.get(VacationPeriod, period_id)
    if period is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Отпуск не найден",
        )
    if period.status == "cancelled":
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Отменённый отпуск нельзя изменить",
        )

    employee = await _get_vacation_employee(session, period.employee_id)
    next_start = date_start or period.date_start
    next_end = date_end or period.date_end
    next_days = await _validate_period_payload(
        session,
        employee=employee,
        date_start=next_start,
        date_end=next_end,
        exclude_period_id=period.id,
    )
    before = _period_snapshot(period)
    conflicts = await _load_shift_conflicts(
        session,
        employee_id=employee.id,
        date_start=next_start,
        date_end=next_end,
    )
    await _handle_shift_conflicts(
        session,
        conflicts,
        force_remove_conflicting_shifts=force_remove_conflicting_shifts,
    )

    period.date_start = next_start
    period.date_end = next_end
    period.days_count = next_days
    if comment is not UNSET:
        period.comment = _clean_comment(comment if isinstance(comment, str) else None)
    period.status = "planned" if period.status == "paid" else period.status
    period.updated_at = datetime.now(UTC)
    await session.flush()
    await _add_audit_action(
        session,
        action_type="vacation_period_update",
        target_id=period.id,
        before=before,
        after=_period_snapshot(period),
        actor=actor,
    )
    await _commit_refresh(session, period)
    return period


async def cancel_vacation_period(
    session: AsyncSession,
    period_id: uuid.UUID,
    *,
    actor: CurrentActor,
) -> VacationPeriod:
    period = await session.get(VacationPeriod, period_id)
    if period is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Отпуск не найден",
        )
    before = _period_snapshot(period)
    period.status = "cancelled"
    period.updated_at = datetime.now(UTC)
    await session.flush()
    await _add_audit_action(
        session,
        action_type="vacation_period_cancel",
        target_id=period.id,
        before=before,
        after=_period_snapshot(period),
        actor=actor,
    )
    await _commit_refresh(session, period)
    return period


async def list_vacations(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID | None = None,
    year: int | None = None,
    include_cancelled: bool = False,
) -> list[VacationPeriod]:
    statement = select(VacationPeriod)
    if employee_id is not None:
        statement = statement.where(VacationPeriod.employee_id == employee_id)
    if year is not None:
        statement = statement.where(
            VacationPeriod.date_start >= date(year, 1, 1),
            VacationPeriod.date_start <= date(year, 12, 31),
        )
    if not include_cancelled:
        statement = statement.where(VacationPeriod.status != "cancelled")
    statement = statement.order_by(VacationPeriod.date_start, VacationPeriod.created_at)
    result = await session.scalars(statement)
    periods = list(result.all())
    return [
        period
        for period in periods
        if (employee_id is None or period.employee_id == employee_id)
        and (include_cancelled or period.status != "cancelled")
        and (year is None or period.date_start.year == year)
    ]


async def get_vacation_balance(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    year: int,
) -> VacationBalance:
    await _get_vacation_employee(session, employee_id)
    limit = await vacation_days_per_year_limit(session)
    periods = await list_vacations(
        session,
        employee_id=employee_id,
        year=year,
        include_cancelled=False,
    )
    active_periods = [period for period in periods if period.status in ACTIVE_VACATION_STATUSES]
    used = sum(period.days_count for period in active_periods)
    return VacationBalance(
        employee_id=employee_id,
        year=year,
        limit=limit,
        used=used,
        remaining=max(limit - used, 0),
        periods=active_periods,
    )


async def vacation_days_for_payroll_period(
    session: AsyncSession,
    *,
    period_start: date,
    period_end: date,
) -> dict[tuple[uuid.UUID, date], None]:
    periods = await _active_vacations_overlapping(session, period_start, period_end)
    days: dict[tuple[uuid.UUID, date], None] = {}
    for period in periods:
        current = max(period.date_start, period_start)
        last = min(period.date_end, period_end)
        while current <= last:
            days[(period.employee_id, current)] = None
            current += timedelta(days=1)
    return days


async def employee_has_active_vacation(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    business_date: date,
) -> bool:
    if session is None or not hasattr(session, "scalars"):
        return False
    result = await session.scalars(
        select(VacationPeriod).where(
            VacationPeriod.employee_id == employee_id,
            VacationPeriod.status.in_(ACTIVE_VACATION_STATUSES),
            VacationPeriod.date_start <= business_date,
            VacationPeriod.date_end >= business_date,
        )
    )
    return any(
        (
            isinstance(period, VacationPeriod)
            and period.employee_id == employee_id
            and period.status in ACTIVE_VACATION_STATUSES
            and period.date_start <= business_date <= period.date_end
        )
        for period in result.all()
    )


async def mark_vacations_paid_for_payroll_period(
    session: AsyncSession,
    *,
    period_start: date,
    period_end: date,
) -> int:
    periods = await _active_vacations_overlapping(session, period_start, period_end)
    now = datetime.now(UTC)
    count = 0
    for period in periods:
        if period.status != "planned":
            continue
        if period.date_end > period_end:
            continue
        period.status = "paid"
        period.updated_at = now
        count += 1
    if count:
        await session.flush()
    return count


async def vacation_days_per_year_limit(session: AsyncSession) -> int:
    setting = await _setting(session, VACATION_DAYS_LIMIT_KEY)
    if setting is None:
        return DEFAULT_DAYS_PER_YEAR_LIMIT
    try:
        return int(setting.value)
    except (TypeError, ValueError):
        return DEFAULT_DAYS_PER_YEAR_LIMIT


async def vacation_daily_amount(session: AsyncSession) -> Decimal:
    setting = await _setting(session, VACATION_DAILY_AMOUNT_KEY)
    if setting is None:
        return DEFAULT_DAILY_AMOUNT
    try:
        return Decimal(str(setting.value))
    except Exception:  # noqa: BLE001 - setting fallback must be resilient
        return DEFAULT_DAILY_AMOUNT


async def _validate_period_payload(
    session: AsyncSession,
    *,
    employee: Employee,
    date_start: date,
    date_end: date,
    exclude_period_id: uuid.UUID | None,
) -> int:
    if date_end < date_start:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Дата окончания не может быть раньше даты начала",
        )
    if date_start.year != date_end.year:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Период не может пересекать границу года",
        )
    days_count = (date_end - date_start).days + 1
    if days_count > MAX_DAYS_PER_PERIOD:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Один отпуск можно оформить максимум на {MAX_DAYS_PER_PERIOD} дней.",
        )
    await _ensure_no_vacation_overlap(
        session,
        employee_id=employee.id,
        date_start=date_start,
        date_end=date_end,
        exclude_period_id=exclude_period_id,
    )
    await _ensure_min_gap_between_vacations(
        session,
        employee_id=employee.id,
        date_start=date_start,
        date_end=date_end,
        days_count=days_count,
        exclude_period_id=exclude_period_id,
    )
    await _ensure_annual_limit(
        session,
        employee_id=employee.id,
        year=date_start.year,
        days_count=days_count,
        exclude_period_id=exclude_period_id,
    )
    return days_count


async def _get_vacation_employee(session: AsyncSession, employee_id: uuid.UUID) -> Employee:
    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Сотрудник не найден",
        )
    if employee.status != "active" or employee.position not in VACATION_EMPLOYEE_POSITIONS:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Отпуск можно назначить только активным Поварам и Кассирам",
        )
    return employee


async def _ensure_no_vacation_overlap(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    date_start: date,
    date_end: date,
    exclude_period_id: uuid.UUID | None,
) -> None:
    statement = select(VacationPeriod).where(
        VacationPeriod.employee_id == employee_id,
        VacationPeriod.status != "cancelled",
        VacationPeriod.date_start <= date_end,
        VacationPeriod.date_end >= date_start,
    )
    if exclude_period_id is not None:
        statement = statement.where(VacationPeriod.id != exclude_period_id)
    result = await session.scalars(statement)
    for period in result.all():
        if exclude_period_id is not None and period.id == exclude_period_id:
            continue
        if period.status != "cancelled" and _ranges_overlap(
            period.date_start,
            period.date_end,
            date_start,
            date_end,
        ):
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Период пересекается с уже заведённым отпуском",
            )


async def _ensure_min_gap_between_vacations(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    date_start: date,
    date_end: date,
    days_count: int,
    exclude_period_id: uuid.UUID | None,
) -> None:
    periods = await list_vacations(
        session,
        employee_id=employee_id,
        year=None,
        include_cancelled=False,
    )
    active_periods = [
        period
        for period in periods
        if period.status in ACTIVE_VACATION_STATUSES
        and (exclude_period_id is None or period.id != exclude_period_id)
    ]
    candidate_id = exclude_period_id or uuid.uuid4()
    candidate = VacationPeriod(
        id=candidate_id,
        employee_id=employee_id,
        date_start=date_start,
        date_end=date_end,
        days_count=days_count,
        status="planned",
    )
    ordered_periods = sorted(
        [*active_periods, candidate],
        key=lambda period: (period.date_start, period.date_end),
    )
    for previous, next_period in zip(ordered_periods, ordered_periods[1:], strict=False):
        if previous.id != candidate_id and next_period.id != candidate_id:
            continue
        next_allowed_start = _add_months(
            previous.date_end,
            MIN_MONTHS_BETWEEN_PERIODS,
        )
        if next_period.date_start < next_allowed_start:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Следующий отпуск можно начать не раньше "
                    f"{next_allowed_start.isoformat()}: должно пройти "
                    f"{MIN_MONTHS_BETWEEN_PERIODS} месяца после предыдущего отпуска."
                ),
            )


async def _ensure_annual_limit(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    year: int,
    days_count: int,
    exclude_period_id: uuid.UUID | None,
) -> None:
    limit = await vacation_days_per_year_limit(session)
    periods = await list_vacations(
        session,
        employee_id=employee_id,
        year=year,
        include_cancelled=False,
    )
    used = sum(
        period.days_count
        for period in periods
        if period.status in ACTIVE_VACATION_STATUSES
        and (exclude_period_id is None or period.id != exclude_period_id)
    )
    if used + days_count > limit:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Превышен годовой лимит отпуска: "
                f"использовано {used}, новый период {days_count}, лимит {limit}."
            ),
        )


async def _load_shift_conflicts(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    date_start: date,
    date_end: date,
) -> list[ShiftConflict]:
    result = await session.execute(
        select(ScheduledShift, ShiftSchedule)
        .join(ShiftSchedule, ShiftSchedule.id == ScheduledShift.shift_schedule_id)
        .where(
            ScheduledShift.employee_id == employee_id,
            ScheduledShift.business_date >= date_start,
            ScheduledShift.business_date <= date_end,
            ShiftSchedule.status.in_(("draft", "published")),
        )
        .order_by(ScheduledShift.business_date)
    )
    conflicts = []
    for shift, schedule in result.all():
        if (
            shift.employee_id == employee_id
            and date_start <= shift.business_date <= date_end
            and schedule.status in {"draft", "published"}
        ):
            conflicts.append(
                ShiftConflict(
                    shift_id=shift.id,
                    business_date=shift.business_date,
                    schedule_id=schedule.id,
                    schedule_status=schedule.status,
                )
            )
    return conflicts


async def _handle_shift_conflicts(
    session: AsyncSession,
    conflicts: list[ShiftConflict],
    *,
    force_remove_conflicting_shifts: bool,
) -> None:
    if not conflicts:
        return
    if not force_remove_conflicting_shifts:
        raise VacationShiftConflictError(conflicts)
    if any(conflict.schedule_status == "published" for conflict in conflicts):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Опубликованный график содержит конфликтующие смены. Создайте новую версию.",
        )
    for conflict in conflicts:
        shift = await session.get(ScheduledShift, conflict.shift_id)
        if shift is not None:
            await session.delete(shift)
    await session.flush()


async def _active_vacations_overlapping(
    session: AsyncSession,
    period_start: date,
    period_end: date,
) -> list[VacationPeriod]:
    result = await session.scalars(
        select(VacationPeriod)
        .where(
            VacationPeriod.status.in_(ACTIVE_VACATION_STATUSES),
            VacationPeriod.date_start <= period_end,
            VacationPeriod.date_end >= period_start,
        )
        .order_by(VacationPeriod.employee_id, VacationPeriod.date_start)
    )
    return [
        period
        for period in result.all()
        if period.status in ACTIVE_VACATION_STATUSES
        and _ranges_overlap(period.date_start, period.date_end, period_start, period_end)
    ]


async def _setting(session: AsyncSession, key: str) -> AppSetting | None:
    return await session.scalar(select(AppSetting).where(AppSetting.key == key))


async def _add_audit_action(
    session: AsyncSession,
    *,
    action_type: str,
    target_id: uuid.UUID,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    actor: CurrentActor,
) -> None:
    now = datetime.now(UTC)
    run = AgentRun(
        id=uuid.uuid4(),
        agent_name="vacation_period",
        finished_at=now,
        status="success",
        params={
            "actor_roles": sorted(actor.roles),
            "actor_user_id": str(actor.user_id) if actor.user_id else None,
        },
        result={"action_type": action_type},
    )
    session.add(run)
    await session.flush()
    session.add(
        AgentAction(
            id=uuid.uuid4(),
            agent_run_id=run.id,
            action_type=action_type,
            target_table="vacation_period",
            target_id=target_id,
            before_value=before,
            after_value=after,
        )
    )


def shift_conflicts_to_dicts(conflicts: Iterable[ShiftConflict]) -> list[dict[str, Any]]:
    return [
        {
            "shift_id": conflict.shift_id,
            "business_date": conflict.business_date,
            "schedule_id": conflict.schedule_id,
            "schedule_status": conflict.schedule_status,
        }
        for conflict in conflicts
    ]


def _period_snapshot(period: VacationPeriod) -> dict[str, Any]:
    return {
        "id": str(period.id),
        "employee_id": str(period.employee_id),
        "date_start": period.date_start.isoformat(),
        "date_end": period.date_end.isoformat(),
        "days_count": period.days_count,
        "status": period.status,
        "comment": period.comment,
    }


async def _commit_refresh(session: AsyncSession, period: VacationPeriod) -> None:
    await session.commit()
    await session.refresh(period)


def _clean_comment(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _actor_user_id(actor: CurrentActor) -> uuid.UUID | None:
    return actor.user_id if isinstance(actor.user_id, uuid.UUID) else None


def _ranges_overlap(
    left_start: date,
    left_end: date,
    right_start: date,
    right_end: date,
) -> bool:
    return left_start <= right_end and left_end >= right_start


def _add_months(value: date, months: int) -> date:
    target_month_index = value.month - 1 + months
    target_year = value.year + target_month_index // 12
    target_month = target_month_index % 12 + 1
    target_day = min(value.day, calendar.monthrange(target_year, target_month)[1])
    return date(target_year, target_month, target_day)

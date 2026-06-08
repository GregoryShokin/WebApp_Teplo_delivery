from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor
from app.models import (
    Employee,
    ScheduledShift,
    ShiftAllowanceOverride,
    ShiftLedgerEntry,
    ShiftSchedule,
)
from app.services.employee_effective_events import get_allowances_on_date, get_position_on_date

CASHIER_POSITION = "Кассир"
CASHIER_ALLOWANCE_ASSIGNMENTS_CONFIG_KEY = "payroll.cashier_allowance_assignments_by_date"


@dataclass(slots=True)
class AllowanceCandidate:
    employee_id: uuid.UUID
    full_name: str
    is_senior: bool
    is_deputy_senior: bool
    is_planned: bool
    is_actual: bool
    minutes_worked: int


@dataclass(slots=True)
class AllowanceAssignment:
    recipient_employee_id: uuid.UUID | None
    recipient_role: str
    reason: str
    candidates_snapshot: list[dict[str, Any]]


def resolve_cashier_allowance_recipient(
    *,
    candidates: list[AllowanceCandidate],
    manual_override: ShiftAllowanceOverride | None,
) -> AllowanceAssignment:
    """
    Applies only to cashiers. This function is pure: no database reads or writes.
    Cooks must keep the independent senior/deputy senior allowance logic.
    """
    snapshot = [_candidate_dict(candidate) for candidate in candidates]

    if manual_override is not None:
        if manual_override.recipient_role == "none":
            return AllowanceAssignment(None, "none", "manual_override", snapshot)
        recipient = next(
            (
                candidate
                for candidate in candidates
                if candidate.employee_id == manual_override.recipient_employee_id
            ),
            None,
        )
        if recipient and (recipient.is_planned or recipient.is_actual):
            return AllowanceAssignment(
                recipient.employee_id,
                manual_override.recipient_role,
                "manual_override",
                snapshot,
            )

    seniors = [candidate for candidate in candidates if candidate.is_senior]
    deputies = [candidate for candidate in candidates if candidate.is_deputy_senior]

    if len(seniors) == 1 and not deputies:
        return AllowanceAssignment(seniors[0].employee_id, "senior", "sole_senior", snapshot)
    if not seniors and len(deputies) == 1:
        return AllowanceAssignment(
            deputies[0].employee_id,
            "deputy_senior",
            "sole_deputy",
            snapshot,
        )

    if seniors and deputies:
        senior = seniors[0]
        deputy = deputies[0]
        if senior.is_planned and not deputy.is_planned:
            return AllowanceAssignment(senior.employee_id, "senior", "plan_priority", snapshot)
        if deputy.is_planned and not senior.is_planned:
            return AllowanceAssignment(
                deputy.employee_id,
                "deputy_senior",
                "plan_priority",
                snapshot,
            )
        reason = "manual_override_fallback" if manual_override else "default_senior"
        return AllowanceAssignment(senior.employee_id, "senior", reason, snapshot)

    return AllowanceAssignment(None, "none", "no_candidate", snapshot)


async def load_cashier_candidates(
    session: AsyncSession,
    *,
    business_date: date,
    use_plan: bool,
    use_fact: bool,
    shift_schedule_id: uuid.UUID | None,
) -> list[AllowanceCandidate]:
    planned: dict[uuid.UUID, int] = {}
    actual: dict[uuid.UUID, int] = {}
    employees: dict[uuid.UUID, Employee] = {}

    if use_plan and shift_schedule_id is not None:
        for shift, employee in await _load_planned_cashier_rows(
            session,
            shift_schedule_id=shift_schedule_id,
            business_date=business_date,
        ):
            planned[employee.id] = planned.get(employee.id, 0) + _planned_minutes(shift)
            employees[employee.id] = employee

    if use_fact:
        for ledger_entry, employee in await _load_actual_cashier_rows(
            session,
            business_date=business_date,
        ):
            actual[employee.id] = actual.get(employee.id, 0) + _ledger_minutes(ledger_entry)
            employees[employee.id] = employee

    candidates: list[AllowanceCandidate] = []
    for employee_id in sorted(set(planned) | set(actual), key=lambda item: str(item)):
        employee = employees.get(employee_id)
        if employee is None:
            continue
        if await _effective_position(session, employee, business_date) != CASHIER_POSITION:
            continue
        allowance_flags = await _effective_allowances(session, employee, business_date)
        is_senior = bool(allowance_flags.get("is_senior"))
        is_deputy_senior = bool(allowance_flags.get("is_deputy_senior"))
        if not is_senior and not is_deputy_senior:
            continue
        candidates.append(
            AllowanceCandidate(
                employee_id=employee.id,
                full_name=employee.full_name,
                is_senior=is_senior,
                is_deputy_senior=is_deputy_senior,
                is_planned=employee.id in planned,
                is_actual=employee.id in actual,
                minutes_worked=actual.get(employee.id) or planned.get(employee.id, 0),
            )
        )
    return candidates


async def get_cashier_override(
    session: AsyncSession,
    *,
    shift_schedule_id: uuid.UUID,
    business_date: date,
) -> ShiftAllowanceOverride | None:
    result = await session.scalars(
        select(ShiftAllowanceOverride).where(
            ShiftAllowanceOverride.shift_schedule_id == shift_schedule_id,
            ShiftAllowanceOverride.business_date == business_date,
            ShiftAllowanceOverride.position == CASHIER_POSITION,
        )
    )
    return next(
        (
            item
            for item in result.all()
            if item.shift_schedule_id == shift_schedule_id
            and item.business_date == business_date
            and item.position == CASHIER_POSITION
        ),
        None,
    )


async def list_cashier_overrides(
    session: AsyncSession,
    *,
    shift_schedule_id: uuid.UUID,
    business_date: date | None = None,
) -> list[ShiftAllowanceOverride]:
    statement = select(ShiftAllowanceOverride).where(
        ShiftAllowanceOverride.shift_schedule_id == shift_schedule_id,
        ShiftAllowanceOverride.position == CASHIER_POSITION,
    )
    if business_date is not None:
        statement = statement.where(ShiftAllowanceOverride.business_date == business_date)
    statement = statement.order_by(ShiftAllowanceOverride.business_date)
    result = await session.scalars(statement)
    return [
        item
        for item in result.all()
        if item.shift_schedule_id == shift_schedule_id
        and item.position == CASHIER_POSITION
        and (business_date is None or item.business_date == business_date)
    ]


async def upsert_cashier_override(
    session: AsyncSession,
    *,
    shift_schedule_id: uuid.UUID,
    business_date: date,
    recipient_role: str,
    recipient_employee_id: uuid.UUID | None,
    comment: str | None,
    actor: CurrentActor,
) -> ShiftAllowanceOverride:
    existing = await get_cashier_override(
        session,
        shift_schedule_id=shift_schedule_id,
        business_date=business_date,
    )
    if existing is None:
        existing = ShiftAllowanceOverride(
            id=uuid.uuid4(),
            shift_schedule_id=shift_schedule_id,
            business_date=business_date,
            position=CASHIER_POSITION,
        )
        session.add(existing)

    existing.recipient_role = recipient_role
    existing.recipient_employee_id = recipient_employee_id
    existing.comment = comment
    existing.set_by_user_id = _actor_user_id(actor)
    existing.set_at = datetime.now(UTC)
    existing.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(existing)
    return existing


async def patch_cashier_override(
    session: AsyncSession,
    *,
    shift_schedule_id: uuid.UUID,
    override_id: uuid.UUID,
    business_date: date,
    recipient_role: str,
    recipient_employee_id: uuid.UUID | None,
    comment: str | None,
    actor: CurrentActor,
) -> ShiftAllowanceOverride:
    override = await session.get(ShiftAllowanceOverride, override_id)
    if (
        override is None
        or override.shift_schedule_id != shift_schedule_id
        or override.position != CASHIER_POSITION
    ):
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Override надбавки не найден",
        )
    override.business_date = business_date
    override.recipient_role = recipient_role
    override.recipient_employee_id = recipient_employee_id
    override.comment = comment
    override.set_by_user_id = _actor_user_id(actor)
    override.set_at = datetime.now(UTC)
    override.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(override)
    return override


async def delete_cashier_override(
    session: AsyncSession,
    *,
    shift_schedule_id: uuid.UUID,
    override_id: uuid.UUID,
) -> None:
    override = await session.get(ShiftAllowanceOverride, override_id)
    if (
        override is None
        or override.shift_schedule_id != shift_schedule_id
        or override.position != CASHIER_POSITION
    ):
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Override надбавки не найден",
        )
    await session.delete(override)
    await session.commit()


async def latest_published_schedule_id_covering(
    session: AsyncSession,
    business_date: date,
) -> uuid.UUID | None:
    result = await session.scalars(
        select(ShiftSchedule)
        .where(
            ShiftSchedule.status == "published",
            ShiftSchedule.date_start <= business_date,
            ShiftSchedule.date_end >= business_date,
        )
        .order_by(ShiftSchedule.published_at.desc().nullslast(), ShiftSchedule.created_at.desc())
    )
    schedules = [
        schedule
        for schedule in result.all()
        if schedule.status == "published"
        and schedule.date_start <= business_date <= schedule.date_end
    ]
    return schedules[0].id if schedules else None


async def resolve_cashier_allowance_for_day(
    session: AsyncSession,
    *,
    business_date: date,
    shift_schedule_id: uuid.UUID | None,
    use_plan: bool,
    use_fact: bool,
) -> AllowanceAssignment:
    candidates = await load_cashier_candidates(
        session,
        business_date=business_date,
        use_plan=use_plan,
        use_fact=use_fact,
        shift_schedule_id=shift_schedule_id,
    )
    override = (
        await get_cashier_override(
            session,
            shift_schedule_id=shift_schedule_id,
            business_date=business_date,
        )
        if shift_schedule_id is not None
        else None
    )
    return resolve_cashier_allowance_recipient(
        candidates=candidates,
        manual_override=override,
    )


def assignment_to_dict(assignment: AllowanceAssignment) -> dict[str, Any]:
    return {
        "recipient_employee_id": assignment.recipient_employee_id,
        "recipient_full_name": recipient_full_name(assignment),
        "recipient_role": assignment.recipient_role,
        "reason": assignment.reason,
        "candidates": assignment.candidates_snapshot,
    }


def recipient_full_name(assignment: AllowanceAssignment) -> str | None:
    if assignment.recipient_employee_id is None:
        return None
    for candidate in assignment.candidates_snapshot:
        if candidate.get("employee_id") == str(assignment.recipient_employee_id):
            return str(candidate.get("full_name") or "")
    return None


async def _load_planned_cashier_rows(
    session: AsyncSession,
    *,
    shift_schedule_id: uuid.UUID,
    business_date: date,
) -> list[tuple[ScheduledShift, Employee]]:
    result = await session.execute(
        select(ScheduledShift, Employee)
        .join(Employee, Employee.id == ScheduledShift.employee_id)
        .where(
            ScheduledShift.shift_schedule_id == shift_schedule_id,
            ScheduledShift.business_date == business_date,
            Employee.position == CASHIER_POSITION,
        )
    )
    return [
        (shift, employee)
        for shift, employee in result.all()
        if shift.shift_schedule_id == shift_schedule_id
        and shift.business_date == business_date
        and employee.position == CASHIER_POSITION
    ]


async def _load_actual_cashier_rows(
    session: AsyncSession,
    *,
    business_date: date,
) -> list[tuple[ShiftLedgerEntry, Employee]]:
    result = await session.execute(
        select(ShiftLedgerEntry, Employee)
        .join(Employee, Employee.id == ShiftLedgerEntry.employee_id)
        .where(
            ShiftLedgerEntry.work_date == business_date,
            ShiftLedgerEntry.closed_at.is_not(None),
            Employee.position == CASHIER_POSITION,
        )
    )
    return [
        (entry, employee)
        for entry, employee in result.all()
        if entry.work_date == business_date
        and entry.closed_at is not None
        and employee.position == CASHIER_POSITION
    ]


async def _effective_position(
    session: AsyncSession,
    employee: Employee,
    business_date: date,
) -> str | None:
    return await get_position_on_date(session, employee.id, business_date) or employee.position


async def _effective_allowances(
    session: AsyncSession,
    employee: Employee,
    business_date: date,
) -> dict[str, bool]:
    if await session.get(Employee, employee.id) is None:
        return {
            "is_senior": bool(employee.is_senior),
            "is_deputy_senior": bool(employee.is_deputy_senior),
        }
    flags = await get_allowances_on_date(session, employee.id, business_date)
    return {
        "is_senior": bool(flags.get("is_senior", employee.is_senior)),
        "is_deputy_senior": bool(flags.get("is_deputy_senior", employee.is_deputy_senior)),
    }


def _candidate_dict(candidate: AllowanceCandidate) -> dict[str, Any]:
    return {
        "employee_id": str(candidate.employee_id),
        "full_name": candidate.full_name,
        "is_senior": candidate.is_senior,
        "is_deputy_senior": candidate.is_deputy_senior,
        "is_planned": candidate.is_planned,
        "is_actual": candidate.is_actual,
        "minutes_worked": candidate.minutes_worked,
    }


def _planned_minutes(shift: ScheduledShift) -> int:
    return max(int(round((shift.planned_end_at - shift.planned_start_at).total_seconds() / 60)), 0)


def _ledger_minutes(entry: ShiftLedgerEntry) -> int:
    if entry.closed_at is None:
        return 0
    return max(int((entry.closed_at - entry.opened_at).total_seconds() // 60), 0)


def _actor_user_id(actor: CurrentActor) -> uuid.UUID | None:
    user_id = getattr(actor, "user_id", None)
    return user_id if isinstance(user_id, uuid.UUID) else None

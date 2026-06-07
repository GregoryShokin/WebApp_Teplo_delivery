from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor
from app.models import Employee, EmployeePositionAssignment, PayrollPeriod, PayrollRun
from app.services.staff_taxonomy import (
    AUXILIARY_POSITIONS,
    CREATE_POSITIONS,
    canonical_position_name,
)


class EmployeePositionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PayrollPeriodSummary:
    id: uuid.UUID
    start_date: date
    end_date: date

    def as_payload(self) -> dict[str, str]:
        return {
            "id": str(self.id),
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "label": f"{self.start_date.isoformat()} - {self.end_date.isoformat()}",
        }


class ClosedPayrollPeriodConflict(EmployeePositionError):
    def __init__(self, periods: list[PayrollPeriodSummary]) -> None:
        self.periods = periods
        labels = ", ".join(period.as_payload()["label"] for period in periods)
        super().__init__(
            "Изменение должности затрагивает закрытую неделю. "
            f"Подтвердите корректировку ведомости: {labels}"
        )

    @property
    def detail(self) -> dict[str, Any]:
        return {
            "code": "closed_payroll_period",
            "message": (
                "Изменение должности затрагивает закрытую неделю. "
                "Подтвердите, что потребуется корректировка ведомости."
            ),
            "periods": [period.as_payload() for period in self.periods],
        }


@dataclass(frozen=True, slots=True)
class PositionHistoryMutationResult:
    assignment: EmployeePositionAssignment | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    warnings: list[dict[str, Any]]


async def position_at(
    session: AsyncSession,
    employee_id: uuid.UUID,
    work_date: date,
) -> str | None:
    return await session.scalar(
        select(EmployeePositionAssignment.position)
        .where(
            EmployeePositionAssignment.employee_id == employee_id,
            EmployeePositionAssignment.effective_from <= work_date,
            or_(
                EmployeePositionAssignment.effective_to.is_(None),
                EmployeePositionAssignment.effective_to >= work_date,
            ),
        )
        .order_by(EmployeePositionAssignment.effective_from.desc())
        .limit(1)
    )


async def current_position(session: AsyncSession, employee_id: uuid.UUID) -> str | None:
    return await position_at(session, employee_id, date.today())


async def change_position(
    session: AsyncSession,
    employee_id: uuid.UUID,
    new_position: str,
    *,
    effective_from: date,
    comment: str | None,
    actor: CurrentActor,
    acknowledge_closed_period: bool = False,
) -> EmployeePositionAssignment:
    canonical = canonical_position_name(new_position)
    if canonical not in CREATE_POSITIONS + AUXILIARY_POSITIONS:
        raise EmployeePositionError(f"Неизвестная должность: {new_position}")

    current = await session.scalar(
        select(EmployeePositionAssignment).where(
            EmployeePositionAssignment.employee_id == employee_id,
            EmployeePositionAssignment.effective_to.is_(None),
        )
    )

    if current and current.position == canonical:
        employee = await session.get(Employee, employee_id)
        if employee is not None:
            employee.requires_position_review = False
        return current

    if current and effective_from <= current.effective_from:
        raise EmployeePositionError(
            f"Новая дата {effective_from} должна быть после начала текущей должности "
            f"{current.effective_from}"
        )

    await ensure_no_closed_payroll_conflict(
        session,
        [(effective_from, None)],
        acknowledge_closed_period=acknowledge_closed_period,
    )

    if current:
        current.effective_to = effective_from - timedelta(days=1)

    new_assignment = EmployeePositionAssignment(
        employee_id=employee_id,
        position=canonical,
        effective_from=effective_from,
        comment=comment,
        created_by_user_id=actor.user_id,
    )
    session.add(new_assignment)

    employee = await session.get(Employee, employee_id)
    if employee:
        employee.requires_position_review = False

    await session.flush()
    return new_assignment


async def replace_position_assignment(
    session: AsyncSession,
    employee_id: uuid.UUID,
    assignment_id: uuid.UUID,
    *,
    position: str | None,
    effective_from: date | None,
    comment: str | None,
    actor: CurrentActor,
    acknowledge_closed_period: bool = False,
) -> PositionHistoryMutationResult:
    del actor
    assignments = await _list_position_history_asc(session, employee_id)
    target = _assignment_by_id(assignments, assignment_id)
    canonical = None
    if position is not None:
        canonical = canonical_position_name(position)
        if canonical not in CREATE_POSITIONS + AUXILIARY_POSITIONS:
            raise EmployeePositionError(f"Неизвестная должность: {position}")

    before_by_id = _states_by_id(assignments)
    states = [_assignment_state(assignment) for assignment in assignments]
    target_state = next(state for state in states if state["id"] == assignment_id)
    if canonical is not None:
        target_state["position"] = canonical
    if effective_from is not None:
        target_state["effective_from"] = effective_from
    if comment is not None:
        target_state["comment"] = comment

    _normalize_planned_history(states, original_first_from=assignments[0].effective_from)
    after_by_id = {state["id"]: dict(state) for state in states}
    affected_ranges = _payroll_affected_ranges(before_by_id, after_by_id)
    await ensure_no_closed_payroll_conflict(
        session,
        affected_ranges,
        acknowledge_closed_period=acknowledge_closed_period,
    )
    warnings = await payroll_recalculation_warnings(session, affected_ranges)

    models_by_id = {assignment.id: assignment for assignment in assignments}
    for state in states:
        assignment = models_by_id[state["id"]]
        assignment.position = state["position"]
        assignment.effective_from = state["effective_from"]
        assignment.effective_to = state["effective_to"]
        assignment.comment = state["comment"]

    employee = await session.get(Employee, employee_id)
    if employee is not None:
        employee.requires_position_review = False

    await session.flush()
    return PositionHistoryMutationResult(
        assignment=target,
        before=_public_state(before_by_id[target.id]),
        after=_public_state(after_by_id[target.id]),
        warnings=warnings,
    )


async def delete_position_assignment(
    session: AsyncSession,
    employee_id: uuid.UUID,
    assignment_id: uuid.UUID,
    *,
    comment: str | None,
    actor: CurrentActor,
    acknowledge_closed_period: bool = False,
) -> PositionHistoryMutationResult:
    del actor, comment
    assignments = await _list_position_history_asc(session, employee_id)
    target = _assignment_by_id(assignments, assignment_id)
    if len(assignments) <= 1:
        raise EmployeePositionError("Нельзя удалить единственный интервал должности")

    before_by_id = _states_by_id(assignments)
    states = [
        _assignment_state(assignment)
        for assignment in assignments
        if assignment.id != target.id
    ]
    if target == assignments[0]:
        states[0]["effective_from"] = target.effective_from

    _normalize_planned_history(states, original_first_from=assignments[0].effective_from)
    after_by_id = {state["id"]: dict(state) for state in states}
    affected_ranges = _payroll_affected_ranges(before_by_id, after_by_id)
    await ensure_no_closed_payroll_conflict(
        session,
        affected_ranges,
        acknowledge_closed_period=acknowledge_closed_period,
    )
    warnings = await payroll_recalculation_warnings(session, affected_ranges)

    models_by_id = {assignment.id: assignment for assignment in assignments}
    for state in states:
        assignment = models_by_id[state["id"]]
        assignment.position = state["position"]
        assignment.effective_from = state["effective_from"]
        assignment.effective_to = state["effective_to"]
        assignment.comment = state["comment"]
    await session.delete(target)

    employee = await session.get(Employee, employee_id)
    if employee is not None:
        employee.requires_position_review = False

    await session.flush()
    return PositionHistoryMutationResult(
        assignment=None,
        before=_public_state(before_by_id[target.id]),
        after=None,
        warnings=warnings,
    )


async def list_position_history(
    session: AsyncSession,
    employee_id: uuid.UUID,
) -> list[EmployeePositionAssignment]:
    return list(
        (
            await session.scalars(
                select(EmployeePositionAssignment)
                .where(EmployeePositionAssignment.employee_id == employee_id)
                .order_by(EmployeePositionAssignment.effective_from.desc())
            )
        ).all()
    )


async def ensure_no_closed_payroll_conflict(
    session: AsyncSession,
    ranges: list[tuple[date, date | None]],
    *,
    acknowledge_closed_period: bool,
) -> list[PayrollPeriodSummary]:
    periods = await _payroll_periods_overlapping_ranges(
        session,
        ranges,
        finalized_only=True,
        with_runs_only=False,
    )
    if periods and not acknowledge_closed_period:
        raise ClosedPayrollPeriodConflict(periods)
    return periods


async def payroll_recalculation_warnings(
    session: AsyncSession,
    ranges: list[tuple[date, date | None]],
) -> list[dict[str, Any]]:
    periods = await _payroll_periods_overlapping_ranges(
        session,
        ranges,
        finalized_only=False,
        with_runs_only=True,
    )
    if not periods:
        return []
    return [
        {
            "code": "payroll_recalculation_required",
            "message": "Есть незакрытая ведомость за затронутый период - потребуется пересчёт.",
            "periods": [period.as_payload() for period in periods],
        }
    ]


async def _list_position_history_asc(
    session: AsyncSession,
    employee_id: uuid.UUID,
) -> list[EmployeePositionAssignment]:
    assignments = list(
        (
            await session.scalars(
                select(EmployeePositionAssignment)
                .where(EmployeePositionAssignment.employee_id == employee_id)
                .order_by(EmployeePositionAssignment.effective_from)
            )
        ).all()
    )
    if not assignments:
        raise EmployeePositionError("У сотрудника нет истории должностей")
    return assignments


def _assignment_by_id(
    assignments: list[EmployeePositionAssignment],
    assignment_id: uuid.UUID,
) -> EmployeePositionAssignment:
    assignment = next((item for item in assignments if item.id == assignment_id), None)
    if assignment is None:
        raise EmployeePositionError("Интервал должности не найден")
    return assignment


def _assignment_state(assignment: EmployeePositionAssignment) -> dict[str, Any]:
    return {
        "id": assignment.id,
        "employee_id": assignment.employee_id,
        "position": assignment.position,
        "effective_from": assignment.effective_from,
        "effective_to": assignment.effective_to,
        "comment": assignment.comment,
    }


def _states_by_id(
    assignments: list[EmployeePositionAssignment],
) -> dict[uuid.UUID, dict[str, Any]]:
    return {assignment.id: _assignment_state(assignment) for assignment in assignments}


def _normalize_planned_history(
    states: list[dict[str, Any]],
    *,
    original_first_from: date,
) -> None:
    states.sort(key=lambda state: state["effective_from"])
    if states[0]["effective_from"] > original_first_from:
        raise EmployeePositionError("Нельзя оставить сотрудника без должности до первого интервала")

    for index, state in enumerate(states):
        if index == len(states) - 1:
            state["effective_to"] = None
            continue
        next_from = states[index + 1]["effective_from"]
        if next_from <= state["effective_from"]:
            raise EmployeePositionError(
                "История должностей не может содержать интервалы с одинаковой датой начала"
            )
        state["effective_to"] = next_from - timedelta(days=1)

    if sum(1 for state in states if state["effective_to"] is None) != 1:
        raise EmployeePositionError("В истории должна быть ровно одна открытая должность")


def _payroll_affected_ranges(
    before_by_id: dict[uuid.UUID, dict[str, Any]],
    after_by_id: dict[uuid.UUID, dict[str, Any]],
) -> list[tuple[date, date | None]]:
    ranges: list[tuple[date, date | None]] = []
    for assignment_id in sorted(set(before_by_id) | set(after_by_id), key=str):
        before = before_by_id.get(assignment_id)
        after = after_by_id.get(assignment_id)
        if _payroll_state(before) == _payroll_state(after):
            continue
        if before is not None:
            ranges.append((before["effective_from"], before["effective_to"]))
        if after is not None:
            ranges.append((after["effective_from"], after["effective_to"]))
    return ranges


def _payroll_state(state: dict[str, Any] | None) -> tuple[str, date, date | None] | None:
    if state is None:
        return None
    return (state["position"], state["effective_from"], state["effective_to"])


def _public_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(state["id"]),
        "employee_id": str(state["employee_id"]),
        "position": state["position"],
        "effective_from": state["effective_from"].isoformat(),
        "effective_to": state["effective_to"].isoformat() if state["effective_to"] else None,
        "comment": state["comment"],
    }


async def _payroll_periods_overlapping_ranges(
    session: AsyncSession,
    ranges: list[tuple[date, date | None]],
    *,
    finalized_only: bool,
    with_runs_only: bool,
) -> list[PayrollPeriodSummary]:
    conditions = [
        and_(
            PayrollPeriod.start_date <= _range_end(start, end),
            PayrollPeriod.end_date >= start,
        )
        for start, end in ranges
    ]
    if not conditions:
        return []

    query = select(PayrollPeriod).where(or_(*conditions))
    if finalized_only:
        query = query.where(PayrollPeriod.status == "finalized")
    else:
        query = query.where(PayrollPeriod.status != "finalized")
    if with_runs_only:
        query = query.join(PayrollRun, PayrollRun.period_id == PayrollPeriod.id).distinct()
    query = query.order_by(PayrollPeriod.start_date)
    periods = list((await session.scalars(query)).all())
    return [
        PayrollPeriodSummary(
            id=period.id,
            start_date=period.start_date,
            end_date=period.end_date,
        )
        for period in periods
    ]


def _range_end(effective_from: date, effective_to: date | None) -> date:
    if effective_to is not None:
        return effective_to
    today = date.today()
    return today if today >= effective_from else effective_from

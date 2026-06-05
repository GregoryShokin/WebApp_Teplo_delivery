from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor
from app.models import Employee, EmployeePositionAssignment
from app.services.staff_taxonomy import (
    AUXILIARY_POSITIONS,
    CREATE_POSITIONS,
    canonical_position_name,
)


class EmployeePositionError(ValueError):
    pass


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

    if current:
        if effective_from <= current.effective_from:
            raise EmployeePositionError(
                f"Новая дата {effective_from} должна быть после начала текущей должности "
                f"{current.effective_from}"
            )
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

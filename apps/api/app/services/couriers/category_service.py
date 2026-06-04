from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CourierCategory, CourierCategoryAssignment
from app.services.couriers.common import get_courier_or_404, get_employee_or_404


async def get_current_category(
    session: AsyncSession,
    employee_id: uuid.UUID,
    at_date: date | None = None,
) -> CourierCategory | None:
    current_date = at_date or date.today()
    assignment = await session.scalar(
        select(CourierCategoryAssignment)
        .where(
            CourierCategoryAssignment.employee_id == employee_id,
            CourierCategoryAssignment.effective_from <= current_date,
            or_(
                CourierCategoryAssignment.effective_to.is_(None),
                CourierCategoryAssignment.effective_to >= current_date,
            ),
        )
        .order_by(CourierCategoryAssignment.effective_from.desc())
    )
    return assignment.category if assignment is not None else None


async def assign_category(
    session: AsyncSession,
    employee_id: uuid.UUID,
    category: CourierCategory | str,
    effective_from: date,
    actor_id: uuid.UUID,
) -> CourierCategoryAssignment:
    await get_courier_or_404(session, employee_id)
    await get_employee_or_404(session, actor_id)
    resolved_category = _category_value(category)

    existing_same_day = await session.scalar(
        select(CourierCategoryAssignment).where(
            CourierCategoryAssignment.employee_id == employee_id,
            CourierCategoryAssignment.effective_from == effective_from,
        )
    )
    if existing_same_day is not None:
        existing_same_day.category = resolved_category
        existing_same_day.created_by = actor_id
        await session.flush()
        return existing_same_day

    active = await session.scalar(
        select(CourierCategoryAssignment)
        .where(
            CourierCategoryAssignment.employee_id == employee_id,
            CourierCategoryAssignment.effective_from < effective_from,
            or_(
                CourierCategoryAssignment.effective_to.is_(None),
                CourierCategoryAssignment.effective_to >= effective_from,
            ),
        )
        .order_by(CourierCategoryAssignment.effective_from.desc())
    )
    if active is not None:
        active.effective_to = effective_from - timedelta(days=1)

    next_assignment = await session.scalar(
        select(CourierCategoryAssignment)
        .where(
            CourierCategoryAssignment.employee_id == employee_id,
            CourierCategoryAssignment.effective_from > effective_from,
        )
        .order_by(CourierCategoryAssignment.effective_from.asc())
    )
    assignment = CourierCategoryAssignment(
        employee_id=employee_id,
        category=resolved_category,
        effective_from=effective_from,
        effective_to=(
            next_assignment.effective_from - timedelta(days=1)
            if next_assignment is not None
            else None
        ),
        created_by=actor_id,
    )
    session.add(assignment)
    await session.flush()
    return assignment


def _category_value(category: CourierCategory | str) -> CourierCategory:
    if isinstance(category, CourierCategory):
        return category
    return CourierCategory(category)

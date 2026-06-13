from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Employee
from app.services.position_registry import courier_positions


async def get_employee_or_404(session: AsyncSession, employee_id: uuid.UUID) -> Employee:
    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Сотрудник не найден")
    return employee


async def get_courier_or_404(session: AsyncSession, employee_id: uuid.UUID) -> Employee:
    employee = await get_employee_or_404(session, employee_id)
    if employee.position not in courier_positions():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Курьер не найден")
    return employee

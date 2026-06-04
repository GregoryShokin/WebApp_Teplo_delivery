from __future__ import annotations

import uuid
from datetime import date

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Employee, EmployeeRoleAssignment

ADMIN_PAYROLL_ROLES = frozenset({"admin", "administrator"})


def is_admin_cashier(employee: Employee) -> bool:
    if employee.position != "Кассир":
        return False

    direct_role = getattr(employee, "payroll_role", None)
    if direct_role in ADMIN_PAYROLL_ROLES:
        return True

    assignments = employee.__dict__.get("role_assignments", [])
    return any(
        assignment.payroll_role in ADMIN_PAYROLL_ROLES
        and assignment.effective_from <= date.today()
        and (assignment.effective_to is None or assignment.effective_to > date.today())
        for assignment in assignments
    )


async def ensure_admin_cashier(session: AsyncSession, employee_id: uuid.UUID) -> Employee:
    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Автор не найден")
    if is_admin_cashier(employee):
        return employee

    today = date.today()
    assignment_id = await session.scalar(
        select(EmployeeRoleAssignment.id).where(
            EmployeeRoleAssignment.employee_id == employee_id,
            EmployeeRoleAssignment.payroll_role.in_(ADMIN_PAYROLL_ROLES),
            EmployeeRoleAssignment.effective_from <= today,
            or_(
                EmployeeRoleAssignment.effective_to.is_(None),
                EmployeeRoleAssignment.effective_to > today,
            ),
        )
    )
    if assignment_id is not None and employee.position == "Кассир":
        return employee

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Оценку может создать только Кассир с ролью admin",
    )

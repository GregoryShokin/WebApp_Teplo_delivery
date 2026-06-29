"""Пересчёт кэшированного ``employee.status`` от текущих назначений.

``employee.status`` — хранимая колонка, которую пересчитывает
``employee_assignments._refresh_employee_status`` только при мутациях карточки.
Если правила валидности роль×категория меняются в коде (например, миграция
0146 вернула категорию «Внештатный» для поваров), уже заведённые сотрудники
остаются с прежним кэшем (``requires_setup``) до следующей мутации, а
no-op-сохранение в UI патч не шлёт. Этот скрипт прогоняет канонический
``compute_status`` и приводит кэш в соответствие — без иных побочных эффектов.

Запуск (после деплоя кода + ``alembic upgrade head``):

    docker exec -w /app/apps/api teplo-api python -m app.scripts.recompute_employee_statuses
    # предпросмотр без записи:
    docker exec -w /app/apps/api teplo-api python -m app.scripts.recompute_employee_statuses --dry-run
    # пересчитать всех (а не только requires_setup):
    docker exec -w /app/apps/api teplo-api python -m app.scripts.recompute_employee_statuses --all
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models import Employee
from app.services.employee_assignments import get_assignments
from app.services.employee_status import compute_status, position_group_for_position


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recompute cached employee.status from current role assignments."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="recompute every non-dismissed employee (default: only status='requires_setup')",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="roll back instead of committing (preview the effect)",
    )
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    today = date.today()

    async with AsyncSessionLocal() as session:
        conditions = [Employee.status != "inactive"]
        if not args.all:
            conditions.append(Employee.status == "requires_setup")
        employees = (
            await session.scalars(
                select(Employee).where(*conditions).order_by(Employee.full_name)
            )
        ).all()

        changes: list[tuple[str, str, str]] = []
        for employee in employees:
            assignments = await get_assignments(session, employee.id, today)
            new_status = compute_status(
                employee,
                is_iiko_deleted=employee.status == "inactive",
                position_group=position_group_for_position(employee.position),
                assignments=assignments,
            )
            if new_status != employee.status:
                changes.append((employee.full_name, employee.status, new_status))
                employee.status = new_status

        if args.dry_run:
            await session.rollback()
        else:
            await session.commit()

    for full_name, before, after in changes:
        print(f"  {full_name}: {before} -> {after}")
    suffix = " (dry-run, rolled back)" if args.dry_run else ""
    print(f"Scanned {len(employees)} employee(s), changed {len(changes)}{suffix}.")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()

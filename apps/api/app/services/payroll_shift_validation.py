from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AttendanceEntry, PayrollLine, PayrollPeriod, ShiftLedgerEntry
from app.services.shift_ledger import load_ledger_matrix_rows, resolve_unassigned_entries


async def prepare_period_shift_roles(
    session: AsyncSession, period: PayrollPeriod
) -> list[ShiftLedgerEntry]:
    """Подставить поздно назначенную единственную роль до загрузки явок."""
    if period.period_type != "week" or period.status == "finalized":
        return []
    entries = (
        await session.scalars(
            select(ShiftLedgerEntry).where(
                ShiftLedgerEntry.work_date.between(period.start_date, period.end_date)
            )
        )
    ).all()
    await resolve_unassigned_entries(session, entries)
    return list(entries)


async def collect_period_shift_issues(
    session: AsyncSession,
    period: PayrollPeriod,
    *,
    attendance_entries: Iterable[AttendanceEntry] | None = None,
    run_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Сверить весь производственный табель, включая исключённые из явок смены.

    Для финализации дополнительно проверяем, что каждая смена и выбранная роль
    действительно вошли в сохранённый расчёт. Окладный и курьерский контуры сюда
    не входят: набор строк совпадает с производственным Учётом смен.
    """
    if period.period_type != "week":
        return []
    rows = await load_ledger_matrix_rows(session, period.start_date, period.end_date)
    if not rows:
        return []
    if attendance_entries is None:
        attendance_entries = (
            await session.scalars(
                select(AttendanceEntry).where(AttendanceEntry.period_id == period.id)
            )
        ).all()
    attendance_days = {(entry.employee_id, entry.work_date) for entry in attendance_entries}

    calculated_roles: dict[tuple[uuid.UUID, date], set[str]] = {}
    vacation_days: set[tuple[uuid.UUID, date]] = set()
    if run_id is not None:
        lines = (
            await session.scalars(select(PayrollLine).where(PayrollLine.run_id == run_id))
        ).all()
        for line in lines:
            for component in (line.components or {}).get("days", []):
                try:
                    key = (line.employee_id, date.fromisoformat(component["date"]))
                except (KeyError, TypeError, ValueError):
                    continue
                calculated_roles.setdefault(key, set()).add(component.get("role") or line.role)
                if component.get("kind") == "vacation":
                    vacation_days.add(key)

    issues: list[dict[str, Any]] = []
    for entry, employee in rows:
        issue: dict[str, Any] = {
            "employee_id": str(employee.id),
            "employee_name": employee.full_name,
            "work_date": entry.work_date.isoformat(),
            "ledger_entry_id": str(entry.id),
        }
        if not entry.is_resolved or not entry.payroll_role or not entry.category:
            issues.append(
                issue
                | {"type": "unresolved_shift", "message": "Не выбрана роль или категория смены"}
            )
        elif (employee.id, entry.work_date) not in attendance_days:
            issues.append(
                issue
                | {
                    "type": "missing_shift_attendance",
                    "message": "Смена не вошла в расчёт. Пересчитайте ведомость",
                }
            )
        elif (
            run_id is not None
            and (employee.id, entry.work_date) not in vacation_days
            and entry.payroll_role
            not in calculated_roles.get((employee.id, entry.work_date), set())
        ):
            issues.append(
                issue
                | {
                    "type": "stale_shift_calculation",
                    "message": "Роль или смена изменились после расчёта. Пересчитайте ведомость",
                }
            )
    return issues

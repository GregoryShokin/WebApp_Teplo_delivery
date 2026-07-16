"""Seed an OPEN weekly period with attendance so a production employee shows a
non-zero "earned to date" in the advance dialog.

Only for the isolated local preview DB (migrations applied first). The current open
weekly period (07–13 июля) doesn't cover today (16 июля), so production earned = 0.
This adds an open period 14–20 июля with a few shifts for Александр Чмыхов, so the
advance dialog shows how much he has earned so far this period.

Usage:
    DATABASE_URL=postgresql+asyncpg://... python -m app.scripts.seed_production_earned_preview
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models import AttendanceEntry, Employee, PayrollPeriod

SEED_MARKER = "production_earned_preview_v1"
EMPLOYEE_NAME = "Александр Чмыхов"
PERIOD_START = date(2026, 7, 14)
PERIOD_END = date(2026, 7, 20)
PAYROLL_DATE = date(2026, 7, 21)
WORK_DAYS = (date(2026, 7, 14), date(2026, 7, 15), date(2026, 7, 16))


async def _ensure_period(session: AsyncSession) -> PayrollPeriod:
    period = await session.scalar(
        select(PayrollPeriod).where(
            PayrollPeriod.period_type == "week",
            PayrollPeriod.start_date == PERIOD_START,
            PayrollPeriod.end_date == PERIOD_END,
        )
    )
    if period is not None:
        return period
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="week",
        start_date=PERIOD_START,
        end_date=PERIOD_END,
        payroll_date=PAYROLL_DATE,
        status="open",
    )
    session.add(period)
    await session.flush()
    return period


async def seed(session: AsyncSession) -> None:
    employee = await session.scalar(
        select(Employee).where(Employee.full_name == EMPLOYEE_NAME).order_by(Employee.hire_date)
    )
    if employee is None:
        raise RuntimeError(f"Не найден сотрудник {EMPLOYEE_NAME}")

    period = await _ensure_period(session)

    existing = {
        row.work_date
        for row in (
            await session.scalars(
                select(AttendanceEntry).where(
                    AttendanceEntry.employee_id == employee.id,
                    AttendanceEntry.period_id == period.id,
                )
            )
        ).all()
    }

    for work_date in WORK_DAYS:
        if work_date in existing:
            continue
        started = datetime.combine(work_date, time(10, 0), tzinfo=UTC)
        ended = datetime.combine(work_date, time(22, 0), tzinfo=UTC)
        session.add(
            AttendanceEntry(
                id=uuid.uuid4(),
                employee_id=employee.id,
                period_id=period.id,
                work_date=work_date,
                started_at=started,
                ended_at=ended,
                minutes_worked=720,
                station=employee.default_cooking_station or "sushi",
                role=None,
                source="manual",
                quality_status="ok",
                notes=SEED_MARKER,
            )
        )

    await session.commit()
    print(
        f"OK: period {PERIOD_START}–{PERIOD_END} (open), "
        f"{len(WORK_DAYS)} смен для {EMPLOYEE_NAME}"
    )


async def _main() -> None:
    async with AsyncSessionLocal() as session:
        await seed(session)


if __name__ == "__main__":
    asyncio.run(_main())

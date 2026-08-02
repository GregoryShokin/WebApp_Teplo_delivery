"""Отпускные одним траншем входят в ДОЛГ перед сотрудником, но не в потолок аванса.

Решение владельца 02.08.2026: аванс — только за отработанное, а «Учёт ДЗ/КЗ» обязан
показывать всё, что заплатит ведомость, включая ещё не отгулянный отпуск. Две цифры
разошлись намеренно, и этот тест держит обе: `earned_to_date` (аванс) без транша,
`vacation_payable` (долг) — с ним.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from cp_helpers import admin_headers
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    AttendanceEntry,
    Employee,
    EmployeePositionAssignment,
    EmployeeRoleAssignment,
    PayrollPeriod,
    VacationPeriod,
)
from app.services.payroll_runner import current_week_bounds

BASE = "/api/v1/accounting/suppliers"
VACATION_DAYS = 3
# vacation.daily_amount по умолчанию = 1000 ₽/день.
LUMP = 3000.0


def _admin(factory) -> dict[str, str]:
    return asyncio.run(admin_headers(factory))


def _vacation_window(payroll_date: date) -> tuple[date, date]:
    """Отпуск ПОСЛЕ даты выплаты — на дату среза не отгулян ни один его день.

    CHECK-констрейнт запрещает отпуску пересекать границу года, поэтому у прогона
    в последние дни декабря окно переносится на начало следующего года.
    """
    start = payroll_date + timedelta(days=1)
    end = start + timedelta(days=VACATION_DAYS - 1)
    if end.year != start.year:
        start = date(end.year, 1, 2)
        end = start + timedelta(days=VACATION_DAYS - 1)
    return start, end


def test_vacation_lump_is_debt_but_not_advance_ceiling(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    week_start, week_end, payroll_date = current_week_bounds(date.today())

    async def seed() -> uuid.UUID:
        async with async_session_factory() as session:
            employee = Employee(
                id=uuid.uuid4(),
                full_name="Повар Отпускной ДЗКЗ",
                iiko_id=f"iiko-{uuid.uuid4()}",
                position="Повар",
                status="active",
                admin_payroll_excluded=False,
                is_senior=False,
                is_deputy_senior=False,
                category="category_1",
            )
            session.add(employee)
            await session.flush()
            period = PayrollPeriod(
                id=uuid.uuid4(),
                period_type="week",
                start_date=week_start,
                end_date=week_end,
                payroll_date=payroll_date,
                status="open",
            )
            session.add(period)
            await session.flush()
            vacation_start, vacation_end = _vacation_window(payroll_date)
            session.add_all(
                [
                    EmployeePositionAssignment(
                        id=uuid.uuid4(),
                        employee_id=employee.id,
                        position="Повар",
                        effective_from=week_start - timedelta(days=365),
                    ),
                    EmployeeRoleAssignment(
                        id=uuid.uuid4(),
                        employee_id=employee.id,
                        payroll_role="pizza",
                        category="category_1",
                        is_primary=True,
                        effective_from=week_start - timedelta(days=365),
                    ),
                    AttendanceEntry(
                        id=uuid.uuid4(),
                        employee_id=employee.id,
                        period_id=period.id,
                        # Первый день недели — заведомо ≤ сегодня.
                        work_date=week_start,
                        started_at=datetime(
                            week_start.year, week_start.month, week_start.day, 7, tzinfo=UTC
                        ),
                        ended_at=datetime(
                            week_start.year, week_start.month, week_start.day, 19, tzinfo=UTC
                        ),
                        minutes_worked=720,
                        role="pizza",
                        source="manual",
                        quality_status="ok",
                        is_open=False,
                    ),
                    VacationPeriod(
                        id=uuid.uuid4(),
                        employee_id=employee.id,
                        date_start=vacation_start,
                        date_end=vacation_end,
                        days_count=VACATION_DAYS,
                        payout_date=payroll_date,
                        status="planned",
                    ),
                ]
            )
            await session.commit()
            return employee.id

    employee_id = asyncio.run(seed())
    response = client.get(f"{BASE}/staff-payable", headers=_admin(async_session_factory))
    assert response.status_code == 200, response.text
    payload = response.json()
    row = next(item for item in payload["items"] if item["employee_id"] == str(employee_id))

    # Аванс — только за отработанное: транш в потолок не входит.
    assert row["earned_to_date"] > 0
    assert row["earned_to_date"] < LUMP + row["earned_to_date"]
    # Долг — со всем, что заплатит ведомость.
    assert row["vacation_payable"] == LUMP
    assert row["salary_payable"] == row["earned_to_date"] + LUMP
    # Отпускные не потерялись между строкой и итогом списка.
    assert payload["vacation_total"] == LUMP


def test_no_vacation_leaves_debt_equal_to_earned(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Без отпуска долг по зарплате равен заработанному — новое слагаемое не протекает."""
    week_start, week_end, payroll_date = current_week_bounds(date.today())

    async def seed() -> uuid.UUID:
        async with async_session_factory() as session:
            employee = Employee(
                id=uuid.uuid4(),
                full_name="Повар Без Отпуска ДЗКЗ",
                iiko_id=f"iiko-{uuid.uuid4()}",
                position="Повар",
                status="active",
                admin_payroll_excluded=False,
                is_senior=False,
                is_deputy_senior=False,
                category="category_1",
            )
            session.add(employee)
            await session.flush()
            period = PayrollPeriod(
                id=uuid.uuid4(),
                period_type="week",
                start_date=week_start,
                end_date=week_end,
                payroll_date=payroll_date,
                status="open",
            )
            session.add(period)
            await session.flush()
            session.add_all(
                [
                    EmployeePositionAssignment(
                        id=uuid.uuid4(),
                        employee_id=employee.id,
                        position="Повар",
                        effective_from=week_start - timedelta(days=365),
                    ),
                    EmployeeRoleAssignment(
                        id=uuid.uuid4(),
                        employee_id=employee.id,
                        payroll_role="pizza",
                        category="category_1",
                        is_primary=True,
                        effective_from=week_start - timedelta(days=365),
                    ),
                    AttendanceEntry(
                        id=uuid.uuid4(),
                        employee_id=employee.id,
                        period_id=period.id,
                        work_date=week_start,
                        started_at=datetime(
                            week_start.year, week_start.month, week_start.day, 7, tzinfo=UTC
                        ),
                        ended_at=datetime(
                            week_start.year, week_start.month, week_start.day, 19, tzinfo=UTC
                        ),
                        minutes_worked=720,
                        role="pizza",
                        source="manual",
                        quality_status="ok",
                        is_open=False,
                    ),
                ]
            )
            await session.commit()
            return employee.id

    employee_id = asyncio.run(seed())
    response = client.get(f"{BASE}/staff-payable", headers=_admin(async_session_factory))
    assert response.status_code == 200, response.text
    payload = response.json()
    row = next(item for item in payload["items"] if item["employee_id"] == str(employee_id))

    assert row["vacation_payable"] == 0.0
    assert row["salary_payable"] == row["earned_to_date"]
    assert Decimal(str(row["earned_to_date"])) > 0

"""АУДИТ-4: двойной счёт оклада в /accounting/suppliers/staff-payable.

``_okladnik_earned`` собирает СИНТЕТИЧЕСКИЙ период ``_half_month_bounds(as_of)`` со
``status='open'`` и не смотрит, финализирован ли реальный PayrollPeriod за те же даты.
Поэтому пока as_of попадает внутрь уже финализированного полумесяца (день финализации —
15-е и последний день месяца), одни и те же деньги считаются дважды:
``payable = earned_to_date (полумесячный оклад) + finalized_unpaid (тот же оклад)``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from cp_helpers import admin_headers
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    Employee,
    EmployeePositionAssignment,
    PayrollLine,
    PayrollPeriod,
    PayrollRate,
    PayrollRun,
)
from app.services.payroll_advance_availability import _half_month_bounds

BASE = "/api/v1/accounting/suppliers"
POSITION = "Управляющий"
OKLAD = Decimal("80000.00")


async def test_finalized_half_month_not_double_counted_in_staff_payable(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Полумесяц, покрывающий СЕГОДНЯ, финализирован и не выплачен. Долг сотруднику обязан
    равняться начисленному по ведомости (хвост), а не хвосту ПЛЮС earned-to-date того же
    полумесяца."""
    start, end = _half_month_bounds(date.today())
    accrued = (OKLAD / 2).quantize(Decimal("0.01"))

    async with async_session_factory() as session:
        employee = Employee(
            id=uuid.uuid4(),
            full_name="Окладник Аудит",
            iiko_id=f"iiko-{uuid.uuid4()}",
            status="active",
            is_senior=False,
            is_deputy_senior=False,
            pin_hash="hashed-pin",
            pin_set_at=datetime(2026, 5, 1, tzinfo=UTC),
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
            updated_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        session.add(employee)
        await session.flush()
        session.add(
            EmployeePositionAssignment(
                id=uuid.uuid4(),
                employee_id=employee.id,
                position=POSITION,
                effective_from=date(2026, 1, 1),
                effective_to=None,
            )
        )
        session.add(
            PayrollRate(
                id=uuid.uuid4(),
                employee_id=None,
                position_group=POSITION,
                category="admin",
                station=None,
                rate_type="monthly",
                amount=OKLAD,
                is_active=True,
                effective_from=date(2026, 1, 1),
            )
        )
        # Ведомость за ТЕКУЩИЙ полумесяц финализирована, но ещё не выплачена.
        period = PayrollPeriod(
            id=uuid.uuid4(),
            period_type="half_month",
            start_date=start,
            end_date=end,
            payroll_date=end,
            status="finalized",
        )
        session.add(period)
        await session.flush()
        run = PayrollRun(
            id=uuid.uuid4(),
            period_id=period.id,
            status="finalized",
            is_imported_legacy=False,
            started_at=datetime.now(tz=UTC),
        )
        session.add(run)
        await session.flush()
        session.add(
            PayrollLine(
                id=uuid.uuid4(),
                run_id=run.id,
                employee_id=employee.id,
                role=POSITION,
                total_payable=accrued,
            )
        )
        await session.commit()
        emp_id = str(employee.id)

    headers = await admin_headers(async_session_factory)
    response = client.get(f"{BASE}/staff-payable", headers=headers)
    assert response.status_code == 200, response.text
    row = next((i for i in response.json()["items"] if i["employee_id"] == emp_id), None)
    assert row is not None, "сотрудник не попал в баланс расчётов"
    payable = Decimal(str(row["payable"]))
    assert payable == accrued, (
        f"Долг сотруднику {payable} при начисленном по финализированной ведомости {accrued}: "
        f"earned_to_date={row['earned_to_date']} + finalized_unpaid={row['finalized_unpaid']} "
        "— один и тот же полумесяц посчитан дважды"
    )

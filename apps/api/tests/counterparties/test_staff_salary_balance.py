"""Полный зарплатный баланс сотрудников для «Учёта ДЗ/КЗ»."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from cp_helpers import admin_headers
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    AppSetting,
    Employee,
    EmployeePayout,
    EmployeePositionAssignment,
    PayrollLine,
    PayrollPeriod,
    PayrollRate,
    PayrollRun,
    SalaryAdvance,
)

BASE = "/api/v1/accounting/suppliers"


def _admin(factory) -> dict[str, str]:
    return asyncio.run(admin_headers(factory))


def _employee(name: str, *, position: str | None = None, excluded: bool = False) -> Employee:
    return Employee(
        id=uuid.uuid4(),
        full_name=name,
        iiko_id=f"iiko-{uuid.uuid4()}",
        position=position,
        status="active",
        admin_payroll_excluded=excluded,
        is_senior=False,
        is_deputy_senior=False,
    )


def _on_demand_line(
    run_id: uuid.UUID, employee_id: uuid.UUID, *, month: str, amount: str
) -> PayrollLine:
    return PayrollLine(
        id=uuid.uuid4(),
        run_id=run_id,
        employee_id=employee_id,
        role="Управляющий",
        base_pay=Decimal("0"),
        premium=Decimal("0"),
        percent_pay=Decimal("0"),
        vacation_pay=Decimal("0"),
        ndfl_withheld=Decimal("0"),
        fund_accrual=Decimal("0"),
        deduction=Decimal("0"),
        total_payable=Decimal("0"),
        deposit_excluded_for_run=False,
        components={
            "kind": "admin_oklad",
            "proration": {
                "on_demand": True,
                "period_month": month,
                "accrual_amount": amount,
            },
        },
    )


def test_on_demand_debt_replaces_current_half_month_proration(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Григорий: 280k начислено − 140k выплачено = 140k КЗ; текущий прорейт не добавляется."""

    async def seed() -> uuid.UUID:
        today = date.today()
        prev_year = today.year if today.month > 1 else today.year - 1
        prev_month = today.month - 1 if today.month > 1 else 12
        async with async_session_factory() as session:
            employee = _employee("Григорий Баланс", position="Управляющий")
            session.add(employee)
            await session.flush()
            session.add_all(
                [
                    EmployeePositionAssignment(
                        id=uuid.uuid4(),
                        employee_id=employee.id,
                        position="Управляющий",
                        effective_from=date(prev_year, prev_month, 1),
                    ),
                    PayrollRate(
                        id=uuid.uuid4(),
                        employee_id=employee.id,
                        position_group="Управляющий",
                        category="admin",
                        station=None,
                        rate_type="monthly",
                        amount=Decimal("140000"),
                        is_active=True,
                        effective_from=date(prev_year, prev_month, 1),
                    ),
                    AppSetting(
                        key="payroll.okladnik_payout_modes",
                        value={"Управляющий": "on_demand"},
                        value_type="object",
                        category="payroll",
                        display_name="Режим выплаты окладов",
                        widget_type="json",
                    ),
                ]
            )
            for year, month in ((prev_year, prev_month), (today.year, today.month)):
                period = PayrollPeriod(
                    id=uuid.uuid4(),
                    period_type="half_month",
                    start_date=date(year, month, 1),
                    end_date=date(year, month, 15),
                    payroll_date=date(year, month, 15),
                    status="open",
                )
                run = PayrollRun(
                    id=uuid.uuid4(),
                    period_id=period.id,
                    status="completed",
                    is_imported_legacy=False,
                    started_at=datetime.now(tz=UTC),
                )
                session.add_all([period, run])
                await session.flush()
                session.add(
                    _on_demand_line(
                        run.id,
                        employee.id,
                        month=f"{year:04d}-{month:02d}",
                        amount="140000.00",
                    )
                )
            session.add(
                EmployeePayout(
                    id=uuid.uuid4(),
                    employee_id=employee.id,
                    kind="owner_salary",
                    amount=Decimal("140000"),
                    payout_date=today,
                    status="paid",
                )
            )
            await session.commit()
            return employee.id

    employee_id = asyncio.run(seed())
    response = client.get(f"{BASE}/staff-payable", headers=_admin(async_session_factory))
    assert response.status_code == 200, response.text
    row = next(
        item for item in response.json()["items"] if item["employee_id"] == str(employee_id)
    )

    assert row["basis"] == "on_demand"
    assert row["earned_to_date"] == 0.0
    assert row["on_demand_accrued"] == 280000.0
    assert row["on_demand_paid"] == 140000.0
    assert row["on_demand_debt"] == 140000.0
    assert row["payable"] == 140000.0


def test_employee_receivables_are_broken_down_by_source(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Аванс, заём и выплата вне ведомости видны отдельно и целиком входят в ДЗ."""

    async def seed() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        async with async_session_factory() as session:
            sofia = _employee("София Баланс")
            yuri = _employee("Юрий Баланс")
            payout_employee = _employee("Выплата Баланс")
            session.add_all([sofia, yuri, payout_employee])
            await session.flush()
            session.add_all(
                [
                    SalaryAdvance(
                        employee_id=sofia.id,
                        role="courier",
                        kind="advance",
                        amount=Decimal("1000"),
                        per_installment_amount=Decimal("1000"),
                        installments_count=1,
                        recovered_amount=Decimal("0"),
                        status="issued",
                        issued_on=date.today(),
                    ),
                    SalaryAdvance(
                        employee_id=yuri.id,
                        role="manager",
                        kind="loan",
                        amount=Decimal("10000"),
                        per_installment_amount=Decimal("1000"),
                        installments_count=10,
                        recovered_amount=Decimal("0"),
                        status="issued",
                        issued_on=date.today(),
                    ),
                    EmployeePayout(
                        employee_id=payout_employee.id,
                        kind="salary",
                        amount=Decimal("2500"),
                        offset_amount=Decimal("500"),
                        payout_date=date.today(),
                        status="paid",
                    ),
                ]
            )
            await session.commit()
            return sofia.id, yuri.id, payout_employee.id

    sofia_id, yuri_id, payout_employee_id = asyncio.run(seed())
    response = client.get(f"{BASE}/staff-payable", headers=_admin(async_session_factory))
    assert response.status_code == 200, response.text
    rows = {item["employee_id"]: item for item in response.json()["items"]}

    sofia = rows[str(sofia_id)]
    assert sofia["advances_outstanding"] == 1000.0
    assert sofia["loans_outstanding"] == 0.0
    assert sofia["receivable"] == 1000.0

    yuri = rows[str(yuri_id)]
    assert yuri["advances_outstanding"] == 0.0
    assert yuri["loans_outstanding"] == 10000.0
    assert yuri["receivable"] == 10000.0

    payout = rows[str(payout_employee_id)]
    assert payout["salary_payouts_outstanding"] == 2000.0
    assert payout["receivable"] == 2000.0

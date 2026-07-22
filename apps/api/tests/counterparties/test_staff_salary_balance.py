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
    AccumulationFundAccount,
    AppSetting,
    CourierDepositAccount,
    DepositAccount,
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


def test_no_pay_employee_is_absent_from_salary_balance(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """«Не платить» полностью исключает даже сотрудника с окладом и встречным долгом."""

    async def seed() -> uuid.UUID:
        today = date.today()
        async with async_session_factory() as session:
            employee = _employee(
                "Павел Не платить",
                position="Управляющий",
                excluded=True,
            )
            session.add(employee)
            await session.flush()
            session.add_all(
                [
                    EmployeePositionAssignment(
                        id=uuid.uuid4(),
                        employee_id=employee.id,
                        position="Управляющий",
                        effective_from=date(today.year, 1, 1),
                    ),
                    PayrollRate(
                        id=uuid.uuid4(),
                        employee_id=employee.id,
                        position_group="Управляющий",
                        category="admin",
                        station=None,
                        rate_type="monthly",
                        amount=Decimal("80000"),
                        is_active=True,
                        effective_from=date(today.year, 1, 1),
                    ),
                    SalaryAdvance(
                        employee_id=employee.id,
                        role="manager",
                        kind="loan",
                        amount=Decimal("10000"),
                        per_installment_amount=Decimal("1000"),
                        installments_count=10,
                        recovered_amount=Decimal("0"),
                        status="issued",
                        issued_on=today,
                    ),
                    EmployeePayout(
                        employee_id=employee.id,
                        kind="salary",
                        amount=Decimal("5000"),
                        offset_amount=Decimal("0"),
                        payout_date=today,
                        status="paid",
                    ),
                ]
            )
            await session.commit()
            return employee.id

    employee_id = asyncio.run(seed())
    response = client.get(f"{BASE}/staff-payable", headers=_admin(async_session_factory))
    assert response.status_code == 200, response.text
    assert str(employee_id) not in {
        item["employee_id"] for item in response.json()["items"]
    }


def test_staff_balance_combines_fund_and_both_deposit_ledgers_without_duplicate_senior_courier(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Производство объединяется с фондом, обычный курьер идёт отдельной строкой,
    а старший курьер получает одну строку с зарплатой и курьерским депозитом.
    """

    async def seed() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        current_year = date.today().year
        async with async_session_factory() as session:
            production = _employee("Повар с обязательствами", position="Повар", excluded=True)
            courier = _employee("Обычный курьер с депозитом", position="Курьер")
            senior = _employee("Старший курьер с зарплатой", position="Старший курьер")
            session.add_all([production, courier, senior])
            await session.flush()

            session.add_all(
                [
                    EmployeePositionAssignment(
                        id=uuid.uuid4(),
                        employee_id=production.id,
                        position="Повар",
                        effective_from=date(2026, 1, 1),
                    ),
                    EmployeePositionAssignment(
                        id=uuid.uuid4(),
                        employee_id=courier.id,
                        position="Курьер",
                        effective_from=date(2026, 1, 1),
                    ),
                    EmployeePositionAssignment(
                        id=uuid.uuid4(),
                        employee_id=senior.id,
                        position="Старший курьер",
                        effective_from=date(2026, 1, 1),
                    ),
                    DepositAccount(
                        id=uuid.uuid4(),
                        employee_id=production.id,
                        balance=Decimal("5000"),
                        initial_balance=Decimal("5000"),
                    ),
                    AccumulationFundAccount(
                        id=uuid.uuid4(),
                        employee_id=production.id,
                        year=current_year,
                        accumulated_amount=Decimal("10000"),
                        paid_out_amount=Decimal("1000"),
                        forfeited_amount=Decimal("1000"),
                        status="active",
                    ),
                    AccumulationFundAccount(
                        id=uuid.uuid4(),
                        employee_id=production.id,
                        year=current_year - 1,
                        accumulated_amount=Decimal("2000"),
                        paid_out_amount=Decimal("0"),
                        forfeited_amount=Decimal("0"),
                        status="active",
                    ),
                    CourierDepositAccount(
                        employee_id=courier.id,
                        target_amount_cents=500_000,
                        opening_balance_cents=300_000,
                        opening_date=date(2026, 1, 1),
                    ),
                    CourierDepositAccount(
                        employee_id=senior.id,
                        target_amount_cents=500_000,
                        opening_balance_cents=400_000,
                        opening_date=date(2026, 1, 1),
                    ),
                ]
            )

            period = PayrollPeriod(
                id=uuid.uuid4(),
                period_type="half_month",
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 15),
                payroll_date=date(2026, 1, 15),
                status="finalized",
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
                PayrollLine(
                    id=uuid.uuid4(),
                    run_id=run.id,
                    employee_id=senior.id,
                    role="Старший курьер",
                    base_pay=Decimal("10000"),
                    premium=Decimal("0"),
                    percent_pay=Decimal("0"),
                    vacation_pay=Decimal("0"),
                    ndfl_withheld=Decimal("0"),
                    fund_accrual=Decimal("0"),
                    deduction=Decimal("0"),
                    total_payable=Decimal("10000"),
                    deposit_excluded_for_run=False,
                    components={},
                )
            )
            await session.commit()
            return production.id, courier.id, senior.id

    production_id, courier_id, senior_id = asyncio.run(seed())
    response = client.get(f"{BASE}/staff-payable", headers=_admin(async_session_factory))
    assert response.status_code == 200, response.text
    payload = response.json()
    rows = {item["employee_id"]: item for item in payload["items"]}

    production = rows[str(production_id)]
    assert production["staff_group"] == "staff"
    assert production["salary_payable"] == 0.0  # «Не платить» действует только на зарплату.
    assert production["fund_payable"] == 10000.0
    assert production["fund_current_year_payable"] == 8000.0
    assert production["fund_prior_years_payable"] == 2000.0
    assert production["production_deposit_payable"] == 5000.0
    assert production["courier_deposit_payable"] == 0.0
    assert production["payable"] == 15000.0

    courier = rows[str(courier_id)]
    assert courier["staff_group"] == "courier"
    assert courier["basis"] == "courier_deposit"
    assert courier["salary_payable"] == 0.0
    assert courier["courier_deposit_payable"] == 3000.0
    assert courier["payable"] == 3000.0

    senior = rows[str(senior_id)]
    assert senior["staff_group"] == "staff"
    assert senior["salary_payable"] == 10000.0
    assert senior["courier_deposit_payable"] == 4000.0
    assert senior["payable"] == 14000.0
    assert [item["employee_id"] for item in payload["items"]].count(str(senior_id)) == 1

    assert payload["fund_total"] >= 10000.0
    assert payload["fund_current_year_total"] >= 8000.0
    assert payload["fund_prior_years_total"] >= 2000.0
    assert payload["fund_total"] == (
        payload["fund_current_year_total"] + payload["fund_prior_years_total"]
    )
    assert payload["production_deposit_total"] >= 5000.0
    assert payload["courier_deposit_total"] >= 7000.0
    assert payload["deposit_total"] == (
        payload["production_deposit_total"] + payload["courier_deposit_total"]
    )


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

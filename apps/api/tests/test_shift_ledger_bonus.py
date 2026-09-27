from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from test_access_rights_backend_guards import _headers_for_permissions
from test_daily_percent_service import _new_week

from app.api.v1.routes import shifts as routes
from app.models import (
    AppSetting,
    AttendanceEntry,
    Employee,
    EmployeeRoleAssignment,
    ShiftLedgerEntry,
)
from app.schemas.payroll import ShiftLedgerBonusesRead
from app.services import shift_ledger_bonus as service
from app.services.daily_percent_service import compute_daily_percent_for_date
from app.services.payroll_calculator import DAILY_REVENUE_CONFIG_KEY

DAY = date(2026, 7, 8)
NOW = datetime(2026, 7, 8, 20, tzinfo=UTC)


def moment(hour: int) -> datetime:
    return datetime(2026, 7, 8, hour, tzinfo=UTC)


async def employee_shift(session, *, role="sushi", category="category_1", start=7, end=19):
    employee = Employee(
        id=uuid.uuid4(),
        full_name=f"Сотрудник {uuid.uuid4()}",
        iiko_id=str(uuid.uuid4()),
        position="Кассир" if role == "administrator" else "Повар",
    )
    session.add(employee)
    await session.flush()
    assignment = EmployeeRoleAssignment(
        employee_id=employee.id,
        payroll_role=role,
        category=category,
        is_primary=True,
        effective_from=date(2026, 1, 1),
    )
    shift = ShiftLedgerEntry(
        employee_id=employee.id,
        work_date=DAY,
        payroll_role=role,
        category=category,
        opened_at=moment(start),
        closed_at=moment(end) if end is not None else None,
        is_resolved=True,
        source="fallback_primary",
    )
    session.add_all([assignment, shift])
    await session.flush()
    return employee, assignment, shift


@pytest.fixture
def revenue(monkeypatch):
    fetch = AsyncMock(return_value={DAY: Decimal("140000")})
    monkeypatch.setattr(service, "fetch_daily_revenue", fetch)
    return fetch


def current_day(result):
    # Validates the actual API serialization too (UUIDs / Decimals / timestamps).
    ShiftLedgerBonusesRead.model_validate(result)
    return next(day for day in result["days"] if day["date"] == DAY)


def amounts(day):
    return {row["employee_id"]: row["percent"] for row in day["employees"]}


async def test_live_revenue_without_payroll_run_and_no_writes(async_session_factory, revenue):
    async with async_session_factory() as session:
        cook, _, _ = await employee_shift(session)
        cashier, _, _ = await employee_shift(session, role="administrator", category="category_2")
        session.add(
            AppSetting(
                key=DAILY_REVENUE_CONFIG_KEY,
                value={DAY.isoformat(): "40000"},
                value_type="object",
                category="payroll",
                display_name="Выручка",
                widget_type="json",
            )
        )
        await session.commit()
        day = current_day(await service.calculate_ledger_bonuses(session, DAY, now=NOW))
        assert day["daily_revenue"] == Decimal("140000.00")
        assert day["percent_pool"] == Decimal("6300")
        assert amounts(day) == {cook.id: Decimal("3600"), cashier.id: Decimal("2700")}
        revenue.assert_awaited_once_with(session, DAY - timedelta(days=6), DAY)
        assert await session.scalar(select(func.count()).select_from(AttendanceEntry)) == 0
        assert (
            await session.scalar(
                select(AppSetting).where(AppSetting.key == DAILY_REVENUE_CONFIG_KEY)
            )
        ).value == {DAY.isoformat(): "40000"}
        revenue.return_value = {DAY: Decimal("190000")}
        updated = current_day(await service.calculate_ledger_bonuses(session, DAY, now=NOW))
        assert updated["percent_pool"] == Decimal("10450")
        assert amounts(updated)[cook.id] > amounts(day)[cook.id]
        assert not session.new and not session.dirty


async def test_open_shift_uses_elapsed_hours_then_closing_changes_share(
    async_session_factory, revenue
):
    async with async_session_factory() as session:
        first, _, _ = await employee_shift(session, start=0, end=12)
        second, _, shift = await employee_shift(session, start=14, end=None)
        await session.commit()
        day = current_day(await service.calculate_ledger_bonuses(session, DAY, now=NOW))
        assert day["has_open_shifts"] is True
        assert amounts(day) == {first.id: Decimal("4200"), second.id: Decimal("2100")}
        shift.closed_at = moment(17)
        await session.commit()
        day = current_day(await service.calculate_ledger_bonuses(session, DAY, now=NOW))
        assert not day["has_open_shifts"]
        assert amounts(day) == {first.id: Decimal("5040"), second.id: Decimal("1260")}


async def test_overlapping_shifts_and_category_history_match_existing_daily_calculator(
    async_session_factory, revenue
):
    async with async_session_factory() as session:
        cook, assignment, shift = await employee_shift(session)
        other, _, other_shift = await employee_shift(session)
        # The current ledger snapshot is stale; historical staff assignments win.
        shift.category = "category_4"
        assignment.effective_to = DAY + timedelta(days=1)
        session.add(
            EmployeeRoleAssignment(
                employee_id=cook.id,
                payroll_role="sushi",
                category="category_4",
                is_primary=True,
                effective_from=DAY + timedelta(days=1),
            )
        )
        duplicate = ShiftLedgerEntry(
            employee_id=cook.id,
            work_date=DAY,
            payroll_role="sushi",
            category="category_4",
            opened_at=moment(15),
            closed_at=moment(19),
            is_resolved=True,
            source="schedule",
        )
        session.add(duplicate)
        period = await _new_week(session)
        for ledger in [shift, duplicate, other_shift]:
            session.add(
                AttendanceEntry(
                    employee_id=ledger.employee_id,
                    period_id=period.id,
                    work_date=DAY,
                    started_at=ledger.opened_at,
                    ended_at=ledger.closed_at,
                    minutes_worked=int((ledger.closed_at - ledger.opened_at).total_seconds() / 60),
                    role=ledger.payroll_role,
                    source="manual",
                    quality_status="ok",
                )
            )
        session.add(
            AppSetting(
                key=DAILY_REVENUE_CONFIG_KEY,
                value={DAY.isoformat(): "140000"},
                value_type="object",
                category="payroll",
                display_name="Выручка",
                widget_type="json",
            )
        )
        await session.commit()
        day = current_day(await service.calculate_ledger_bonuses(session, DAY, now=NOW))
        payroll = await compute_daily_percent_for_date(session, DAY)
        assert (
            amounts(day)
            == payroll.per_employee
            == {cook.id: Decimal("3150"), other.id: Decimal("3150")}
        )
        assert (
            next(row for row in day["employees"] if row["employee_id"] == cook.id)["shifts"][0][
                "hours"
            ]
            == 12
        )


async def test_vacations_and_zero_coefficient_do_not_take_share(
    async_session_factory, revenue, monkeypatch
):
    async with async_session_factory() as session:
        cook, _, _ = await employee_shift(session)
        intern, _, _ = await employee_shift(session, category="intern")
        vacation, _, _ = await employee_shift(session)
        monkeypatch.setattr(
            service.vacation_service,
            "vacation_days_for_payroll_period",
            AsyncMock(return_value={(vacation.id, DAY)}),
        )
        await session.commit()
        day = current_day(await service.calculate_ledger_bonuses(session, DAY, now=NOW))
        assert amounts(day) == {
            cook.id: Decimal("6300"),
            intern.id: Decimal("0"),
            vacation.id: Decimal("0"),
        }


@pytest.mark.parametrize("problem", ["unresolved", "invalid_interval", "forgotten_open"])
async def test_incomplete_day_does_not_overstate_other_employees_share(
    async_session_factory, revenue, problem
):
    async with async_session_factory() as session:
        await employee_shift(session)
        _, _, shift = await employee_shift(session)
        now = NOW
        if problem == "unresolved":
            shift.is_resolved = False
            shift.payroll_role = None
        elif problem == "invalid_interval":
            shift.closed_at = moment(6)
        else:
            shift.closed_at = None
            now += timedelta(days=1)
        await session.commit()
        day = current_day(await service.calculate_ledger_bonuses(session, DAY, now=now))
        assert day["status"] == "needs_review"
        assert day["employees"] == []
        assert day["daily_revenue"] == Decimal("140000")


async def test_empty_day_and_zero_revenue_are_valid(async_session_factory, revenue):
    async with async_session_factory() as session:
        revenue.return_value = {}
        result = await service.calculate_ledger_bonuses(session, DAY, now=NOW)
        assert len(result["days"]) == 7
        assert all(day["status"] == "ready" and day["daily_revenue"] == 0 for day in result["days"])
        assert current_day(result)["employees"] == []


@pytest.mark.parametrize("error", [RuntimeError("iiko down"), SystemExit("missing config")])
async def test_unavailable_revenue_is_never_reported_as_zero(async_session_factory, revenue, error):
    async with async_session_factory() as session:
        revenue.side_effect = error
        with pytest.raises(RuntimeError):
            await service.calculate_ledger_bonuses(session, DAY, now=NOW)


def test_bonus_endpoint_permissions_future_date_and_failure(
    client, async_session_factory, monkeypatch
):
    allowed = _headers_for_permissions(
        async_session_factory, "bonus-read@test.local", ["source.shift_ledger.read"]
    )
    denied = _headers_for_permissions(
        async_session_factory, "bonus-denied@test.local", ["source.shift_ledger.input"]
    )
    calculate = AsyncMock(return_value={"calculated_at": NOW, "days": []})
    monkeypatch.setattr(routes, "calculate_ledger_bonuses", calculate)
    url = "/api/v1/shifts/ledger/bonuses"
    assert client.get(url, params={"date": DAY.isoformat()}, headers=denied).status_code == 403
    assert client.get(url, params={"date": "2099-01-01"}, headers=allowed).status_code == 400
    calculate.assert_not_awaited()
    response = client.get(url, params={"date": DAY.isoformat()}, headers=allowed)
    assert response.status_code == 200
    calculate.side_effect = RuntimeError("upstream token must not leak")
    response = client.get(url, params={"date": DAY.isoformat()}, headers=allowed)
    assert response.status_code == 503
    assert "token" not in response.text

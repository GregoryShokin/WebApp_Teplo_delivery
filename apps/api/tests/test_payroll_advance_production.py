"""Производственный earned-to-date (provisional weekly run).

Калькулятор `calculate_payroll_lines` замокан — он покрыт отдельно (142 теста в
test_payroll). Здесь проверяется обвязка `_production_earned`: поиск открытой
недели, усечение явок ≤ as_of, провизорный период с `end = as_of`, суммирование
`total_payable` сотрудника и graceful-ветки (нет недели / нет явок).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    AttendanceEntry,
    Employee,
    EmployeePositionAssignment,
    PayrollLine,
    PayrollPeriod,
)
from app.services import payroll_advance_availability as avail
from app.services.payroll_advance_availability import available_to_advance
from app.services.payroll_calculator import PayrollCalculationResult

WEEK_START = date(2026, 6, 2)
WEEK_END = date(2026, 6, 8)
AS_OF = date(2026, 6, 5)


async def _make_cook(session: AsyncSession) -> Employee:
    employee = Employee(
        id=uuid.uuid4(),
        full_name="Повар Тест",
        iiko_id=f"iiko-{uuid.uuid4()}",
        status="active",
        is_senior=False,
        is_deputy_senior=False,
        hire_date=None,
        fire_date=None,
        pin_hash="hashed-pin",
        pin_set_at=datetime(2026, 1, 1, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add(employee)
    await session.flush()
    session.add(
        EmployeePositionAssignment(
            id=uuid.uuid4(),
            employee_id=employee.id,
            position="Повар",
            effective_from=date(2026, 1, 1),
            effective_to=None,
        )
    )
    await session.flush()
    return employee


async def _make_week(session: AsyncSession) -> PayrollPeriod:
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="week",
        start_date=WEEK_START,
        end_date=WEEK_END,
        payroll_date=date(2026, 6, 9),
        status="open",
    )
    session.add(period)
    await session.flush()
    return period


def _entry(employee_id: uuid.UUID, period_id: uuid.UUID, work_date: date) -> AttendanceEntry:
    return AttendanceEntry(
        id=uuid.uuid4(),
        employee_id=employee_id,
        period_id=period_id,
        work_date=work_date,
        started_at=datetime(work_date.year, work_date.month, work_date.day, 10, tzinfo=UTC),
        ended_at=datetime(work_date.year, work_date.month, work_date.day, 22, tzinfo=UTC),
        minutes_worked=720,
        source="manual",
        quality_status="ok",
    )


async def test_production_earned_sums_total_payable_and_truncates_entries(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    async with async_session_factory() as session:
        cook = await _make_cook(session)
        period = await _make_week(session)
        session.add(_entry(cook.id, period.id, date(2026, 6, 4)))  # ≤ as_of
        session.add(_entry(cook.id, period.id, date(2026, 6, 7)))  # > as_of → отбросить
        await session.commit()

        captured: dict = {}

        async def fake_calc(_session, provisional, _run_id, entries):
            captured["period_end"] = provisional.end_date
            captured["entry_dates"] = sorted(entry.work_date for entry in entries)
            line = PayrollLine(employee_id=cook.id, role="Повар", total_payable=Decimal("3000"))
            return PayrollCalculationResult(lines=[line], blocking_issues=[], summary={})

        monkeypatch.setattr(avail, "calculate_payroll_lines", fake_calc)

        result = await available_to_advance(session, cook, AS_OF)
        assert result.basis == "production"
        assert result.earned_to_date == Decimal("3000.00")
        assert result.available == Decimal("3000.00")
        # AS_OF (05 июн) < payroll_date (09 июн) — день выплаты не наступил.
        assert result.payout_reached is False
        assert (result.period_start, result.period_end) == (WEEK_START, AS_OF)
        # Провизорный период усечён до as_of; явка после as_of отброшена.
        assert captured["period_end"] == AS_OF
        assert captured["entry_dates"] == [date(2026, 6, 4)]


async def test_production_earned_zero_on_payout_date(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    """В день выплаты (as_of == payroll_date) неделя ещё находится по payroll_date,
    заработанное считается (3000), но аванс за неё уже недоступен: заработанное уходит
    сотруднику вместе с ведомостью, поэтому available обнулён и payout_reached=True."""
    async with async_session_factory() as session:
        cook = await _make_cook(session)
        period = await _make_week(session)
        session.add(_entry(cook.id, period.id, WEEK_END))
        session.add(_entry(cook.id, period.id, date(2026, 6, 9)))  # день выплаты, вне недели
        await session.commit()

        captured: dict = {}

        async def fake_calc(_session, provisional, _run_id, entries):
            captured["period_end"] = provisional.end_date
            captured["entry_dates"] = sorted(entry.work_date for entry in entries)
            line = PayrollLine(employee_id=cook.id, role="Повар", total_payable=Decimal("3000"))
            return PayrollCalculationResult(lines=[line], blocking_issues=[], summary={})

        monkeypatch.setattr(avail, "calculate_payroll_lines", fake_calc)

        result = await available_to_advance(session, cook, date(2026, 6, 9))
        assert result.basis == "production"
        assert result.earned_to_date == Decimal("3000.00")
        assert result.available == Decimal("0.00")
        assert result.payout_reached is True
        assert result.note is not None  # «Наступил день выплаты …»
        # Заработанное показываем по ОПЛАЧИВАЕМОМУ периоду (завершившейся неделе [02..08]),
        # а не по дню выплаты (09-е — уже за концом недели).
        assert (result.period_start, result.period_end) == (WEEK_START, WEEK_END)
        assert captured["period_end"] == WEEK_END
        assert captured["entry_dates"] == [WEEK_END]

        # apply_payout_gate=False (реконсиляция уже прошедшей операции) в тот же день
        # выплаты видит полное заработанное — обнулять исторический аванс нельзя.
        via_reconcile = await available_to_advance(
            session, cook, date(2026, 6, 9), apply_payout_gate=False
        )
        assert via_reconcile.available == Decimal("3000.00")
        assert via_reconcile.payout_reached is False


async def test_production_payout_gate_survives_precreated_next_week(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    """Регресс: в день выплаты (09.06) ночной свип уже создал следующую неделю B
    [09..15]; отсечка всё равно срабатывает, т.к. оплачиваемую неделю A ищем по
    payroll_date==as_of, а не по «периоду, содержащему as_of» (иначе вернулась бы B)."""
    async with async_session_factory() as session:
        cook = await _make_cook(session)
        week_a = await _make_week(session)  # [02..08], payroll_date=09
        session.add(
            PayrollPeriod(
                id=uuid.uuid4(),
                period_type="week",
                start_date=date(2026, 6, 9),
                end_date=date(2026, 6, 15),
                payroll_date=date(2026, 6, 16),
                status="open",
            )
        )
        session.add(_entry(cook.id, week_a.id, WEEK_END))
        await session.commit()

        async def fake_calc(_session, provisional, _run_id, entries):
            line = PayrollLine(employee_id=cook.id, role="Повар", total_payable=Decimal("3000"))
            return PayrollCalculationResult(lines=[line], blocking_issues=[], summary={})

        monkeypatch.setattr(avail, "calculate_payroll_lines", fake_calc)

        result = await available_to_advance(session, cook, date(2026, 6, 9))
        assert result.available == Decimal("0.00")
        assert result.payout_reached is True
        assert (result.period_start, result.period_end) == (WEEK_START, WEEK_END)


def test_half_month_payout_paid_end_detects_both_payout_days() -> None:
    """Чистый детект дня выплаты полумесячной ведомости."""
    from app.services.payroll_advance_availability import _half_month_payout_paid_end

    # 15-е — день выплаты 1-го полупериода [01..15].
    assert _half_month_payout_paid_end(date(2026, 1, 15)) == date(2026, 1, 15)
    # 1-е — день выплаты 2-го полупериода ПРЕДЫДУЩЕГО месяца [16..конец].
    assert _half_month_payout_paid_end(date(2026, 2, 1)) == date(2026, 1, 31)
    # Обычные дни (в т.ч. последний день 2-й половины) — не день выплаты.
    assert _half_month_payout_paid_end(date(2026, 1, 14)) is None
    assert _half_month_payout_paid_end(date(2026, 1, 16)) is None
    assert _half_month_payout_paid_end(date(2026, 1, 31)) is None


def test_okladnik_payout_paid_end_respects_mode() -> None:
    """День выплаты окладника зависит от режима: on_demand — нет; first/second_half —
    только свой день; split — обе половины."""
    from app.services.payroll_advance_availability import _okladnik_payout_paid_end

    # split — обе половины.
    assert _okladnik_payout_paid_end("split", date(2026, 1, 15)) == date(2026, 1, 15)
    assert _okladnik_payout_paid_end("split", date(2026, 2, 1)) == date(2026, 1, 31)
    # on_demand — не выплачивается автоматически, дня выплаты нет.
    assert _okladnik_payout_paid_end("on_demand", date(2026, 1, 15)) is None
    assert _okladnik_payout_paid_end("on_demand", date(2026, 2, 1)) is None
    # first_half — только 15-е.
    assert _okladnik_payout_paid_end("first_half", date(2026, 1, 15)) == date(2026, 1, 15)
    assert _okladnik_payout_paid_end("first_half", date(2026, 2, 1)) is None
    # second_half — только 1-е.
    assert _okladnik_payout_paid_end("second_half", date(2026, 2, 1)) == date(2026, 1, 31)
    assert _okladnik_payout_paid_end("second_half", date(2026, 1, 15)) is None


async def test_production_earned_no_attendance_returns_note(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cook = await _make_cook(session)
        await _make_week(session)
        await session.commit()

        result = await available_to_advance(session, cook, AS_OF)
        assert result.basis == "production"
        assert result.earned_to_date == Decimal("0.00")
        assert result.available == Decimal("0.00")
        assert result.note is not None  # «Явки за период ещё не загружены»


async def test_production_earned_no_open_period_returns_note(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cook = await _make_cook(session)
        await session.commit()

        result = await available_to_advance(session, cook, AS_OF)
        assert result.available == Decimal("0.00")
        assert result.note is not None  # «Нет открытого недельного периода»

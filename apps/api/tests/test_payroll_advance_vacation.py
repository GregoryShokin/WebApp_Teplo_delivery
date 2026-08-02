"""Отпускные одним траншем НЕ входят в «доступно к авансу».

Решение владельца 02.08.2026 («аванс только за отработанное»). Лумп-транш отпускных
цепляется к ведомости по ``payroll_date``, а не по окну дат, поэтому провизорный прогон
earned-to-date выдавал полную сумму ещё не отгулянного отпуска с первого дня недели:
у повара с тремя сменами «доступно к авансу» показывало 21 829 ₽, из которых 10 000 ₽ —
отпуск, начинающийся только завтра.

Калькулятор здесь НЕ мокается — именно мок в ``test_payroll_advance_production`` делал
существующие тесты слепыми к этому кейсу.
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
    EmployeeRoleAssignment,
    PayrollPeriod,
    VacationPeriod,
)
from app.services.payroll_advance_availability import available_to_advance
from app.services.payroll_calculator import calculate_payroll_lines, decimal

WEEK_START = date(2026, 6, 2)
WEEK_END = date(2026, 6, 8)
PAYROLL_DATE = date(2026, 6, 9)
AS_OF = date(2026, 6, 5)
# Отпуск начинается ПОСЛЕ as_of: на дату аванса не отгулян ни один его день.
VACATION_START = date(2026, 6, 10)
VACATION_END = date(2026, 6, 19)
VACATION_DAYS = 10
# vacation.daily_amount по умолчанию = 1000 ₽/день.
LUMP = Decimal("10000")


async def _make_cook(session: AsyncSession, name: str) -> Employee:
    employee = Employee(
        id=uuid.uuid4(),
        full_name=name,
        iiko_id=f"iiko-{uuid.uuid4()}",
        status="active",
        is_senior=False,
        is_deputy_senior=False,
        hire_date=None,
        fire_date=None,
        category="category_1",
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
    session.add(
        EmployeeRoleAssignment(
            id=uuid.uuid4(),
            employee_id=employee.id,
            payroll_role="pizza",
            category="category_1",
            is_primary=True,
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
        payroll_date=PAYROLL_DATE,
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
        role="pizza",
        source="manual",
        quality_status="ok",
        is_open=False,
    )


def _vacation(employee_id: uuid.UUID) -> VacationPeriod:
    """Отпуск, выплачиваемый одним траншем ровно в дату выплаты этой ведомости."""
    return VacationPeriod(
        id=uuid.uuid4(),
        employee_id=employee_id,
        date_start=VACATION_START,
        date_end=VACATION_END,
        days_count=VACATION_DAYS,
        payout_date=PAYROLL_DATE,
        status="planned",
    )


def _employee_total(result, employee_id: uuid.UUID) -> Decimal:
    return sum(
        (decimal(line.total_payable) for line in result.lines if line.employee_id == employee_id),
        Decimal("0"),
    )


async def test_lump_paid_by_run_but_dropped_by_flag(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Один и тот же период: ведомость платит транш, провизорный прогон — нет.

    Явок нет вовсе, поэтому вся сумма строки — только отпускные: разница между двумя
    прогонами и есть цена дефекта.
    """
    async with async_session_factory() as session:
        cook = await _make_cook(session, "Повар Отпускной")
        period = await _make_week(session)
        session.add(_vacation(cook.id))
        await session.commit()

        paid = await calculate_payroll_lines(session, period, uuid.uuid4(), [])
        assert _employee_total(paid, cook.id) == LUMP
        assert sum((decimal(line.vacation_pay) for line in paid.lines), Decimal("0")) == LUMP

        earned = await calculate_payroll_lines(
            session, period, uuid.uuid4(), [], include_vacation_payout_lump=False
        )
        assert [line for line in earned.lines if line.employee_id == cook.id] == []


async def test_advance_excludes_lump_and_explains_the_difference(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Смена в авансе есть, отпускные — нет, и разница объяснена нотой."""
    async with async_session_factory() as session:
        cook = await _make_cook(session, "Повар Со Сменой")
        period = await _make_week(session)
        session.add(_entry(cook.id, period.id, date(2026, 6, 4)))
        session.add(_vacation(cook.id))
        await session.commit()

        result = await available_to_advance(session, cook, AS_OF)
        assert result.basis == "production"
        assert result.payout_reached is False

        # Тот же провизорный период, но с траншем — разница ровно на сумму отпускных.
        provisional = PayrollPeriod(
            period_type="week",
            start_date=WEEK_START,
            end_date=AS_OF,
            payroll_date=PAYROLL_DATE,
            status="open",
        )
        entries = [_entry(cook.id, period.id, date(2026, 6, 4))]
        with_lump = await calculate_payroll_lines(session, provisional, uuid.uuid4(), entries)
        assert _employee_total(with_lump, cook.id) - result.earned_to_date == LUMP

        # Смена всё-таки оплачена — доступное не схлопнулось в ноль.
        assert result.earned_to_date > 0
        assert result.available == result.earned_to_date
        assert result.note is not None
        assert "Отпускные" in result.note
        # Неразрывный пробел — как в formatMoney на фронте.
        assert "10\u00a0000\u00a0₽" in result.note
        assert "09.06.2026" in result.note
        # Долг перед сотрудником считается из этого же ответа — «Учёт ДЗ/КЗ» берёт
        # отпускные отсюда, поэтому поле обязано нести полную сумму транша.
        assert result.vacation_payout_lump == LUMP


async def test_advance_zero_when_only_lump_and_no_own_shifts(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Кейс владельца: у сотрудника в периоде нет своих смен, только предстоящий отпуск.

    Раньше чужой явки было достаточно, чтобы провизорный прогон состоялся и выдал ему
    весь транш отпускных. Теперь заработанного ноль — при живом (не заблокированном)
    расчёте, что и подтверждает нота про отпускные вместо «Расчёт заблокирован».
    """
    async with async_session_factory() as session:
        cook = await _make_cook(session, "Повар Без Смен")
        colleague = await _make_cook(session, "Повар Коллега")
        period = await _make_week(session)
        session.add(_entry(colleague.id, period.id, date(2026, 6, 3)))
        session.add(_vacation(cook.id))
        await session.commit()

        result = await available_to_advance(session, cook, AS_OF)
        assert result.earned_to_date == Decimal("0.00")
        assert result.available == Decimal("0.00")
        assert result.note is not None
        assert "Отпускные" in result.note
        assert result.vacation_payout_lump == LUMP


async def test_no_note_without_vacation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Без отпуска пояснения нет — нота не должна засорять обычную выдачу."""
    async with async_session_factory() as session:
        cook = await _make_cook(session, "Повар Без Отпуска")
        period = await _make_week(session)
        session.add(_entry(cook.id, period.id, date(2026, 6, 4)))
        await session.commit()

        result = await available_to_advance(session, cook, AS_OF)
        assert result.note is None
        assert result.earned_to_date > 0
        assert result.vacation_payout_lump == Decimal("0.00")

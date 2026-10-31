"""Долг по штрафу: человек с движением есть в ведомости, неудержанное идёт дальше.

Решения владельца 31.10.2026: любое начисление и удержание попадает в свою неделю; если
удержаний больше начисленного, к выплате идёт ноль, а разница становится долгом и снимается
следующей ведомостью; неделя уволенного показывается, но долгом не ставится; неделя без
движения строки не даёт, долг ждёт ближайшую строку.

Тесты идут настоящим расчётом на настоящей базе: ``run_payroll``, загрузчик явок и табель
смен, ``finalize_payroll_run``, ``unfinalize_payroll_run``, срез и персональный отчёт.
Подменяются только внешний мир (выручка из iiko) и «сегодня» табеля: явки приходят готовым
списком, как их отдал бы iiko.

Равенство контракта держится по человеку за неделю, а не по строке:
``сумма (начислено − НДФЛ − deduction) − penalty_debt_in + penalty_debt_out +
penalty_debt_written_off = сумма total_payable до авансов, зачётов и округления``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_payroll_cash_invariant import _fund_wallet, _reserve_payroll

from app.api.v1.routes.payroll import serialize_payroll_line
from app.models import (
    AppSetting,
    AttendanceEntry,
    DepositAccount,
    DepositPayoutSchedule,
    DepositTransaction,
    Employee,
    EmployeePositionAssignment,
    EmployeeRoleAssignment,
    PayrollAdjustment,
    PayrollAdjustmentCategory,
    PayrollLine,
    PayrollPayment,
    PayrollPayoutBooking,
    PayrollPeriod,
    PayrollRun,
    ShiftLedgerEntry,
    User,
    VacationPeriod,
)
from app.schemas.payroll import PayrollPersonalReportPeriodRead
from app.services import shift_ledger
from app.services.deposit_schedule import DEPOSIT_SCHEDULED_PAYOUT_ENABLED_KEY
from app.services.payroll_calculator import (
    PENALTY_DEBT_DISMISSED_NOTE,
    calculate_payroll_lines,
    latest_penalty_debts,
)
from app.services.payroll_payments import mark_all_payments, mark_payments_selected
from app.services.payroll_personal_report import build_personal_report
from app.services.payroll_runner import (
    PayrollConflictError,
    finalize_payroll_run,
    get_run,
    run_payroll,
    unfinalize_payroll_run,
)
from app.services.position_registry import admin_payroll_positions
from app.services.staff_balance_as_of import StaffBalanceRow, build_staff_balance_as_of

ZERO = Decimal("0.00")
# Первая неделя стенда: вторник, учёт среза начинается 01.07.2026.
FIRST_WEEK_START = date(2026, 7, 7)
# Полная смена по справочнику базы, категория 2. Выручки в тестах нет, поэтому процента нет.
SUSHI_SHIFT = Decimal("2400.00")
PIZZA_SHIFT = Decimal("2200.00")


def cut_outside_world(monkeypatch: pytest.MonkeyPatch) -> None:
    """Отрезать внешний мир: выручка не ходит в iiko, «сегодня» табеля после всех недель."""

    async def no_revenue(*_args: Any, **_kwargs: Any) -> dict[date, Decimal]:
        return {}

    monkeypatch.setattr("app.services.payroll_runner.ensure_daily_revenue_cached", no_revenue)
    monkeypatch.setattr(shift_ledger, "ledger_today", lambda: date(2026, 10, 1))


@pytest.fixture
def stand(monkeypatch: pytest.MonkeyPatch) -> None:
    """Стенд расчёта без внешнего мира."""
    cut_outside_world(monkeypatch)


async def make_actor(session: AsyncSession) -> User:
    """Пользователь, закрывающий ведомости."""
    user = User(
        id=uuid.uuid4(),
        email=f"payroll-debt-{uuid.uuid4()}@test.local",
        hashed_password="hashed",
        full_name="Закрывающий ведомости",
        is_active=True,
    )
    session.add(user)
    await session.commit()
    return user


async def make_week(session: AsyncSession, index: int) -> PayrollPeriod:
    """Неделя стенда вторник–понедельник; ``index`` считает от первой недели."""
    start = FIRST_WEEK_START + timedelta(days=7 * index)
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="week",
        start_date=start,
        end_date=start + timedelta(days=6),
        payroll_date=start + timedelta(days=7),
        status="open",
    )
    session.add(period)
    await session.commit()
    return period


async def make_cook(
    session: AsyncSession,
    name: str,
    *,
    status: str = "active",
    fire_date: date | None = None,
    roles: Iterable[str] = ("sushi",),
    position: str = "Повар",
) -> Employee:
    """Повар с должностью и ролями на всю историю; первая роль — основная."""
    employee = Employee(
        id=uuid.uuid4(),
        full_name=name,
        iiko_id=f"iiko-{uuid.uuid4()}",
        position=position,
        category="category_2",
        status=status,
        fire_date=fire_date,
        hire_date=date(2026, 1, 1),
        tenure_started_at=date(2026, 1, 1),
    )
    session.add(employee)
    await session.flush()
    session.add(
        EmployeePositionAssignment(
            id=uuid.uuid4(),
            employee_id=employee.id,
            position=position,
            effective_from=date(2026, 1, 1),
        )
    )
    for index, role in enumerate(roles):
        session.add(
            EmployeeRoleAssignment(
                id=uuid.uuid4(),
                employee_id=employee.id,
                payroll_role=role,
                category="category_2",
                effective_from=date(2026, 1, 1),
                is_primary=index == 0,
            )
        )
    await session.commit()
    return employee


async def add_shift(
    session: AsyncSession,
    employee: Employee,
    work_date: date,
    *,
    role: str | None = None,
) -> None:
    """Смена в табеле. Роль нужна только человеку с двумя ролями: одну табель ставит сам."""
    session.add(
        ShiftLedgerEntry(
            id=uuid.uuid4(),
            employee_id=employee.id,
            work_date=work_date,
            opened_at=datetime(work_date.year, work_date.month, work_date.day, 7, tzinfo=UTC),
            closed_at=datetime(work_date.year, work_date.month, work_date.day, 19, tzinfo=UTC),
            payroll_role=role,
            category="category_2" if role else None,
            source="manual_correction" if role else "fallback_primary",
            is_resolved=role is not None,
        )
    )
    await session.commit()


async def add_adjustment(
    session: AsyncSession,
    employee: Employee,
    work_date: date,
    kind: str,
    amount: str,
    *,
    role: str = "Повар",
    comment: str = "Запись недели",
) -> PayrollAdjustment:
    """Премия или штраф недели, как их ставит экран: со своей категорией."""
    category = PayrollAdjustmentCategory(
        id=uuid.uuid4(),
        type=kind,
        code=f"test-{uuid.uuid4()}",
        display_name="Запись недели",
    )
    session.add(category)
    await session.flush()
    adjustment = PayrollAdjustment(
        id=uuid.uuid4(),
        employee_id=employee.id,
        work_date=work_date,
        role=role,
        type=kind,
        category_id=category.id,
        amount=Decimal(amount),
        comment=comment,
    )
    session.add(adjustment)
    await session.commit()
    return adjustment


async def attendance_records(session: AsyncSession, period: PayrollPeriod) -> list[dict[str, Any]]:
    """Явки недели в том виде, как их отдаёт iiko, собранные из табеля стенда."""
    rows = await session.execute(
        select(ShiftLedgerEntry, Employee.iiko_id)
        .join(Employee, Employee.id == ShiftLedgerEntry.employee_id)
        .where(
            ShiftLedgerEntry.work_date >= period.start_date,
            ShiftLedgerEntry.work_date <= period.end_date,
        )
    )
    return [
        {
            "employeeId": iiko_id,
            "dateFrom": entry.opened_at.isoformat(),
            "dateTo": entry.closed_at.isoformat(),
            "attendanceType": "Р",
        }
        for entry, iiko_id in rows.all()
    ]


async def calculate(session: AsyncSession, period: PayrollPeriod) -> PayrollRun:
    """Посчитать неделю настоящим расчётом; заблокированный расчёт — ошибка стенда."""
    run = await run_payroll(
        session, period.id, iiko_records=await attendance_records(session, period)
    )
    assert run.status == "completed", run.blocking_issues
    return run


async def close_week(session: AsyncSession, period: PayrollPeriod, actor: User) -> PayrollRun:
    """Посчитать и закрыть неделю."""
    run = await calculate(session, period)
    return await finalize_payroll_run(session, run.id, finalized_by_user_id=actor.id)


async def week_with_colleague(
    session: AsyncSession, index: int, colleague: Employee
) -> PayrollPeriod:
    """Неделя с чужой сменой: без единой явки расчёт блокируется целиком."""
    period = await make_week(session, index)
    await add_shift(session, colleague, period.start_date)
    return period


async def lines_of(
    session: AsyncSession, run: PayrollRun, employee: Employee | None = None
) -> list[PayrollLine]:
    """Строки ведомости свежим чтением, строка-носитель первой не гарантируется."""
    query = select(PayrollLine).where(PayrollLine.run_id == run.id)
    if employee is not None:
        query = query.where(PayrollLine.employee_id == employee.id)
    query = query.order_by(PayrollLine.role).execution_options(populate_existing=True)
    return list((await session.scalars(query)).all())


async def only_line(session: AsyncSession, run: PayrollRun, employee: Employee) -> PayrollLine:
    """Единственная строка человека в ведомости."""
    lines = await lines_of(session, run, employee)
    assert len(lines) == 1, [line.role for line in lines]
    return lines[0]


def payable_before_rounding(lines: Iterable[PayrollLine]) -> Decimal:
    """Сумма к выплате до округления: округление ведомости равенства не касается."""
    total = ZERO
    for line in lines:
        rounding = (line.components or {}).get("payroll_rounding") or {}
        total += line.total_payable + Decimal(str(rounding.get("amount", "0")))
    return total


def assert_week_equality(lines: list[PayrollLine]) -> None:
    """Равенство контракта по человеку за неделю; авансов и зачётов в этих тестах нет."""
    accrued = sum(
        (line.base_pay + line.premium + line.percent_pay + line.vacation_pay for line in lines),
        ZERO,
    )
    withheld = sum((line.ndfl_withheld + line.deduction for line in lines), ZERO)
    debt_in = sum((line.penalty_debt_in for line in lines), ZERO)
    debt_out = sum((line.penalty_debt_out for line in lines), ZERO)
    written_off = sum((line.penalty_debt_written_off for line in lines), ZERO)
    assert accrued - withheld - debt_in + debt_out + written_off == payable_before_rounding(lines)


async def enable_deposit_withholding(session: AsyncSession) -> None:
    """Включить автоудержание депозита: в базе стенда оно выключено."""
    setting = await session.scalar(
        select(AppSetting).where(AppSetting.key == "payroll.deposit_auto_withholding_enabled")
    )
    assert setting is not None
    setting.value = True
    await session.commit()


def balance_row(report: Any, employee: Employee) -> StaffBalanceRow | None:
    """Строка человека в срезе на дату."""
    return next((row for row in report.rows if row.employee_id == employee.id), None)


async def stamp_closing_day(session: AsyncSession, period: PayrollPeriod, day: date) -> None:
    """Поставить день закрытия: срез читает его, а закрывают тесты сегодняшними часами."""
    period.finalized_at = datetime(day.year, day.month, day.day, 12, tzinfo=UTC)
    await session.commit()


# --- строка без смен ---------------------------------------------------------------


async def test_penalty_without_shifts_gives_a_line_with_zero_and_debt(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    async with async_session_factory() as session:
        colleague = await make_cook(session, "Коллега")
        debtor = await make_cook(session, "Должник")
        period = await week_with_colleague(session, 0, colleague)
        penalty = await add_adjustment(
            session, debtor, period.start_date + timedelta(days=2), "penalty", "839.42"
        )

        run = await calculate(session, period)

        line = await only_line(session, run, debtor)
        assert line.role == "sushi"
        assert line.components["days"] == []
        assert line.base_pay == ZERO
        assert line.fund_accrual == ZERO
        assert line.total_payable == ZERO
        assert line.deduction == Decimal("839.42")
        assert line.penalty_debt_in == ZERO
        assert line.penalty_debt_out == Decimal("839.42")
        assert line.penalty_debt_written_off == ZERO
        assert [item["id"] for item in line.components["adjustments"]["penalties"]] == [
            str(penalty.id)
        ]
        assert_week_equality([line])


async def test_line_without_shifts_takes_the_role_of_the_vacation_rule(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    """Роль как у отпускных: основное назначение, а без него запасная роль должности."""
    async with async_session_factory() as session:
        colleague = await make_cook(session, "Коллега")
        without_role = await make_cook(session, "Без назначения", roles=())
        period = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, without_role, period.start_date, "penalty", "100")

        run = await calculate(session, period)

        assert (await only_line(session, run, without_role)).role == "prep"


async def test_bonus_without_shifts_pays_money_and_takes_no_deposit_or_fund(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    async with async_session_factory() as session:
        await enable_deposit_withholding(session)
        colleague = await make_cook(session, "Коллега")
        employee = await make_cook(session, "С премией")
        period = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, employee, period.start_date, "bonus", "1500")

        run = await calculate(session, period)

        line = await only_line(session, run, employee)
        assert line.components["days"] == []
        assert line.premium == Decimal("1500.00")
        assert line.total_payable == Decimal("1500.00")
        assert line.deduction == ZERO
        assert line.components["deposit_withholding"] == 0
        assert line.fund_accrual == ZERO
        assert line.penalty_debt_out == ZERO
        withheld = await session.scalars(
            select(DepositTransaction).where(
                DepositTransaction.run_id == run.id,
                DepositTransaction.employee_id == employee.id,
            )
        )
        assert withheld.all() == []
        assert_week_equality([line])


async def test_admin_role_record_does_not_bring_a_person_into_the_production_week(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    """Ведомость администрации не меняется: её штраф живёт там, а не в недельной."""
    async with async_session_factory() as session:
        colleague = await make_cook(session, "Коллега")
        manager = await make_cook(session, "Управляющий стенда", roles=())
        period = await week_with_colleague(session, 0, colleague)
        await add_adjustment(
            session,
            manager,
            period.start_date,
            "penalty",
            "500",
            role=admin_payroll_positions()[0],
        )

        run = await calculate(session, period)

        assert await lines_of(session, run, manager) == []


async def test_provisional_run_sees_neither_people_without_shifts_nor_the_debt(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    """«Сколько заработано на дату» не снимает долг: срез держит его дебиторкой."""
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        debtor = await make_cook(session, "Должник")
        only_penalty = await make_cook(session, "Только штраф")
        first = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, debtor, first.start_date, "penalty", "900")
        await close_week(session, first, actor)
        second = await week_with_colleague(session, 1, colleague)
        await add_shift(session, debtor, second.start_date)
        await calculate(session, second)
        await add_adjustment(session, only_penalty, second.start_date, "penalty", "50")
        entries = (
            await session.scalars(
                select(AttendanceEntry).where(AttendanceEntry.period_id == second.id)
            )
        ).all()

        result = await calculate_payroll_lines(
            session, second, uuid.uuid4(), entries, include_vacation_payout_lump=False
        )

        by_employee = {line.employee_id: line for line in result.lines}
        assert only_penalty.id not in by_employee
        assert by_employee[debtor.id].penalty_debt_in == ZERO
        assert by_employee[debtor.id].total_payable == SUSHI_SHIFT


# --- цепочка долга -----------------------------------------------------------------


async def test_next_week_withholds_the_whole_debt(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        debtor = await make_cook(session, "Должник")
        first = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, debtor, first.start_date, "penalty", "900")
        await close_week(session, first, actor)
        second = await week_with_colleague(session, 1, colleague)
        await add_shift(session, debtor, second.start_date)

        run = await calculate(session, second)

        line = await only_line(session, run, debtor)
        assert line.penalty_debt_in == Decimal("900.00")
        assert line.penalty_debt_out == ZERO
        assert line.penalty_debt_written_off == ZERO
        # Долг не штраф этой недели: ``deduction`` остаётся штрафами недели плюс депозит.
        assert line.deduction == ZERO
        assert line.total_payable == SUSHI_SHIFT - Decimal("900")
        assert line.components["penalty_debt_taken"] == 900
        assert_week_equality([line])


async def test_debt_bigger_than_the_wage_is_withheld_in_parts(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        debtor = await make_cook(session, "Должник")
        first = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, debtor, first.start_date, "penalty", "5000")
        await close_week(session, first, actor)

        expected = [
            # долг на входе, долг дальше, к выплате
            ("5000.00", "2600.00", "0.00"),
            ("2600.00", "200.00", "0.00"),
            ("200.00", "0.00", "2200.00"),
        ]
        for index, (debt_in, debt_out, payable) in enumerate(expected, start=1):
            week = await week_with_colleague(session, index, colleague)
            await add_shift(session, debtor, week.start_date)
            run = await close_week(session, week, actor)
            line = await only_line(session, run, debtor)
            assert (line.penalty_debt_in, line.penalty_debt_out, line.total_payable) == (
                Decimal(debt_in),
                Decimal(debt_out),
                Decimal(payable),
            )
            assert_week_equality([line])


async def test_week_without_movement_gives_no_line_and_the_debt_waits(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        debtor = await make_cook(session, "Должник")
        first = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, debtor, first.start_date, "penalty", "900")
        await close_week(session, first, actor)
        quiet = await week_with_colleague(session, 1, colleague)
        quiet_run = await close_week(session, quiet, actor)
        third = await week_with_colleague(session, 2, colleague)
        await add_shift(session, debtor, third.start_date)

        run = await calculate(session, third)

        assert await lines_of(session, quiet_run, debtor) == []
        line = await only_line(session, run, debtor)
        assert line.penalty_debt_in == Decimal("900.00")
        assert line.total_payable == SUSHI_SHIFT - Decimal("900")


async def test_recalculating_an_open_week_is_idempotent(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        debtor = await make_cook(session, "Должник")
        first = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, debtor, first.start_date, "penalty", "3000")
        first_run = await close_week(session, first, actor)
        second = await week_with_colleague(session, 1, colleague)
        await add_shift(session, debtor, second.start_date)

        runs = [await calculate(session, second), await calculate(session, second)]

        assert runs[0].id == runs[1].id
        line = await only_line(session, runs[1], debtor)
        assert (line.penalty_debt_in, line.penalty_debt_out, line.total_payable) == (
            Decimal("3000.00"),
            Decimal("600.00"),
            ZERO,
        )
        assert (await only_line(session, first_run, debtor)).penalty_debt_out == Decimal("3000.00")


async def test_recalculation_picks_up_a_penalty_added_after_the_first_count(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    async with async_session_factory() as session:
        colleague = await make_cook(session, "Коллега")
        employee = await make_cook(session, "Смена и штраф")
        week = await week_with_colleague(session, 0, colleague)
        await add_shift(session, employee, week.start_date)
        await calculate(session, week)
        await add_adjustment(session, employee, week.start_date, "penalty", "3000")

        run = await calculate(session, week)

        line = await only_line(session, run, employee)
        assert (line.total_payable, line.penalty_debt_out) == (ZERO, Decimal("600.00"))


async def test_person_with_two_lines_keeps_the_equality_across_lines(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    """Излишек носителя снимается со второй строки, значения стоят на носителе."""
    async with async_session_factory() as session:
        colleague = await make_cook(session, "Коллега")
        two_roles = await make_cook(session, "Две роли", roles=("sushi", "pizza"))
        week = await week_with_colleague(session, 0, colleague)
        await add_shift(session, two_roles, week.start_date, role="sushi")
        await add_shift(session, two_roles, week.start_date + timedelta(days=1), role="pizza")
        await add_adjustment(session, two_roles, week.start_date, "penalty", "3000")

        run = await calculate(session, week)

        lines = {line.role: line for line in await lines_of(session, run, two_roles)}
        assert set(lines) == {"sushi", "pizza"}
        assert lines["sushi"].deduction == Decimal("3000.00")
        assert lines["sushi"].total_payable == ZERO
        assert lines["pizza"].total_payable == PIZZA_SHIFT - Decimal("600")
        assert lines["pizza"].components["penalty_debt_taken"] == 600
        assert lines["sushi"].penalty_debt_out == ZERO
        assert_week_equality(list(lines.values()))


async def test_two_lines_carry_the_remainder_on_one_line_and_take_it_from_both(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        two_roles = await make_cook(session, "Две роли", roles=("sushi", "pizza"))
        first = await week_with_colleague(session, 0, colleague)
        await add_shift(session, two_roles, first.start_date, role="sushi")
        await add_shift(session, two_roles, first.start_date + timedelta(days=1), role="pizza")
        await add_adjustment(session, two_roles, first.start_date, "penalty", "9000")
        first_run = await close_week(session, first, actor)

        lines = {line.role: line for line in await lines_of(session, first_run, two_roles)}
        # Заработано 4600, штраф 9000: всё снято, 4400 уходит дальше, и только с носителя.
        assert lines["sushi"].penalty_debt_out == Decimal("4400.00")
        assert lines["pizza"].penalty_debt_out == ZERO
        assert sum(line.total_payable for line in lines.values()) == 0
        assert_week_equality(list(lines.values()))

        second = await week_with_colleague(session, 1, colleague)
        await add_shift(session, two_roles, second.start_date, role="sushi")
        await add_shift(session, two_roles, second.start_date + timedelta(days=1), role="pizza")
        run = await calculate(session, second)

        lines = {line.role: line for line in await lines_of(session, run, two_roles)}
        assert lines["sushi"].penalty_debt_in == Decimal("4400.00")
        assert lines["pizza"].penalty_debt_in == ZERO
        assert lines["sushi"].total_payable == ZERO
        assert lines["pizza"].total_payable == Decimal("200.00")
        assert lines["sushi"].components["penalty_debt_taken"] == 2400
        assert lines["pizza"].components["penalty_debt_taken"] == 2000
        assert_week_equality(list(lines.values()))


def test_latest_debts_read_one_run_per_person_and_sum_its_lines() -> None:
    """Строки-роли одного прогона складываются, старый пересчёт той же недели — нет."""
    employee_id, settled_id = uuid.uuid4(), uuid.uuid4()
    newest_run, older_run = uuid.uuid4(), uuid.uuid4()

    debts = latest_penalty_debts(
        [
            (employee_id, newest_run, Decimal("100")),
            (employee_id, newest_run, Decimal("50")),
            (employee_id, older_run, Decimal("999")),
            (settled_id, newest_run, Decimal("0")),
            (settled_id, older_run, Decimal("999")),
        ]
    )

    assert debts == {employee_id: Decimal("150")}


# --- штраф больше заработка --------------------------------------------------------


async def test_penalty_bigger_than_the_wage_loses_nothing(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        employee = await make_cook(session, "Смена и штраф")
        week = await week_with_colleague(session, 0, colleague)
        await add_shift(session, employee, week.start_date)
        await add_adjustment(session, employee, week.start_date, "penalty", "4000.55")

        run = await close_week(session, week, actor)

        line = await only_line(session, run, employee)
        assert line.total_payable == ZERO
        assert line.deduction == Decimal("4000.55")
        assert line.penalty_debt_out == Decimal("1600.55")
        assert_week_equality([line])

        next_week = await week_with_colleague(session, 1, colleague)
        await add_shift(session, employee, next_week.start_date)
        next_run = await calculate(session, next_week)
        next_line = await only_line(session, next_run, employee)
        assert next_line.penalty_debt_in == Decimal("1600.55")
        assert next_line.penalty_debt_out == ZERO
        assert_week_equality([next_line])


async def test_debt_and_a_penalty_of_the_same_week_add_up(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        debtor = await make_cook(session, "Должник")
        first = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, debtor, first.start_date, "penalty", "1000")
        await close_week(session, first, actor)
        second = await week_with_colleague(session, 1, colleague)
        await add_shift(session, debtor, second.start_date)
        await add_adjustment(session, debtor, second.start_date, "penalty", "2000")

        run = await calculate(session, second)

        line = await only_line(session, run, debtor)
        assert (line.penalty_debt_in, line.penalty_debt_out) == (
            Decimal("1000.00"),
            Decimal("600.00"),
        )
        assert line.total_payable == ZERO
        assert_week_equality([line])


async def test_person_without_debt_is_not_touched_by_the_settlement(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    """Равенство держится и у обычной строки, а полей долга на ней нет."""
    async with async_session_factory() as session:
        colleague = await make_cook(session, "Коллега")
        employee = await make_cook(session, "Без долга")
        week = await week_with_colleague(session, 0, colleague)
        await add_shift(session, employee, week.start_date)
        await add_adjustment(session, employee, week.start_date, "penalty", "400")

        run = await calculate(session, week)

        line = await only_line(session, run, employee)
        assert line.total_payable == SUSHI_SHIFT - Decimal("400")
        assert (line.penalty_debt_in, line.penalty_debt_out, line.penalty_debt_written_off) == (
            ZERO,
            ZERO,
            ZERO,
        )
        assert "penalty_debt_taken" not in line.components
        assert_week_equality([line])


# --- уволенный ---------------------------------------------------------------------


async def test_dismissed_remainder_is_written_off_and_not_carried(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        dismissed = await make_cook(
            session, "Уволенный", status="inactive", fire_date=date(2026, 7, 8)
        )
        week = await week_with_colleague(session, 0, colleague)
        await add_shift(session, dismissed, week.start_date)
        await add_adjustment(session, dismissed, week.start_date, "penalty", "4000")

        run = await close_week(session, week, actor)
        await stamp_closing_day(session, week, date(2026, 7, 15))

        line = await only_line(session, run, dismissed)
        assert line.total_payable == ZERO
        assert line.penalty_debt_out == ZERO
        assert line.penalty_debt_written_off == Decimal("1600.00")
        assert line.components["penalty_debt_note"] == PENALTY_DEBT_DISMISSED_NOTE
        assert_week_equality([line])
        report = await build_staff_balance_as_of(session, as_of=date(2026, 7, 20))
        row = balance_row(report, dismissed)
        assert row is None or row.penalty_debt == ZERO
        assert report.penalty_debt_total == ZERO


async def test_week_of_a_dismissed_person_without_shifts_is_shown(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    async with async_session_factory() as session:
        colleague = await make_cook(session, "Коллега")
        dismissed = await make_cook(
            session, "Уволенный", status="inactive", fire_date=date(2026, 7, 1)
        )
        week = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, dismissed, week.start_date, "penalty", "839.42")

        run = await calculate(session, week)

        line = await only_line(session, run, dismissed)
        assert line.components["days"] == []
        assert line.penalty_debt_out == ZERO
        assert line.penalty_debt_written_off == Decimal("839.42")
        assert_week_equality([line])


async def test_debt_met_after_dismissal_is_taken_then_written_off(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        debtor = await make_cook(session, "Уволен позже")
        first = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, debtor, first.start_date, "penalty", "900")
        await close_week(session, first, actor)
        debtor.status = "inactive"
        debtor.fire_date = first.end_date
        await session.commit()
        second = await week_with_colleague(session, 1, colleague)
        await add_adjustment(session, debtor, second.start_date, "bonus", "300")

        run = await calculate(session, second)

        line = await only_line(session, run, debtor)
        assert line.penalty_debt_in == Decimal("900.00")
        assert line.penalty_debt_out == ZERO
        assert line.penalty_debt_written_off == Decimal("600.00")
        assert line.total_payable == ZERO
        assert line.components["penalty_debt_note"] == PENALTY_DEBT_DISMISSED_NOTE
        assert_week_equality([line])


async def test_person_being_dismissed_still_carries_the_debt(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    """Списание только у ``inactive``: «в увольнении» расчёт ещё открыт."""
    async with async_session_factory() as session:
        colleague = await make_cook(session, "Коллега")
        leaving = await make_cook(session, "В увольнении", status="dismissing")
        week = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, leaving, week.start_date, "penalty", "500")

        run = await calculate(session, week)

        line = await only_line(session, run, leaving)
        assert (line.penalty_debt_out, line.penalty_debt_written_off) == (Decimal("500.00"), ZERO)


# --- откат и закрытие --------------------------------------------------------------


async def test_rollback_is_refused_when_the_debt_went_into_the_closed_next_week(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        debtor = await make_cook(session, "Должник")
        first = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, debtor, first.start_date, "penalty", "900")
        first_run = await close_week(session, first, actor)
        second = await week_with_colleague(session, 1, colleague)
        await add_shift(session, debtor, second.start_date)
        second_run = await close_week(session, second, actor)

        with pytest.raises(PayrollConflictError) as refused:
            await unfinalize_payroll_run(
                session, first_run.id, reason="Правка штрафа", actor_user_id=actor.id
            )

        assert str(refused.value) == (
            "Откат невозможен: долг этой недели уже перешёл в закрытую следующую"
        )
        await session.refresh(first_run)
        assert first_run.status == "finalized"

        # Снятая сверху неделя освобождает откат нижней.
        await unfinalize_payroll_run(
            session, second_run.id, reason="Сначала следующая", actor_user_id=actor.id
        )
        await unfinalize_payroll_run(
            session, first_run.id, reason="Теперь можно", actor_user_id=actor.id
        )
        await session.refresh(first_run)
        assert first_run.status == "completed"


async def test_rollback_is_allowed_without_debt(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        employee = await make_cook(session, "Без долга")
        first = await week_with_colleague(session, 0, colleague)
        await add_shift(session, employee, first.start_date)
        first_run = await close_week(session, first, actor)
        second = await week_with_colleague(session, 1, colleague)
        await add_shift(session, employee, second.start_date)
        await close_week(session, second, actor)

        await unfinalize_payroll_run(
            session, first_run.id, reason="Правка смены", actor_user_id=actor.id
        )

        await session.refresh(first_run)
        assert first_run.status == "completed"


async def test_rollback_is_allowed_while_the_debt_has_not_moved(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    """Закрытая неделя позже без строки должника долг не приняла: откат не запрещён."""
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        debtor = await make_cook(session, "Должник")
        first = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, debtor, first.start_date, "penalty", "900")
        first_run = await close_week(session, first, actor)
        second = await week_with_colleague(session, 1, colleague)
        await close_week(session, second, actor)

        await unfinalize_payroll_run(
            session, first_run.id, reason="Правка штрафа", actor_user_id=actor.id
        )

        await session.refresh(first_run)
        assert first_run.status == "completed"


async def test_closing_is_refused_when_the_carried_debt_went_stale(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    """Предыдущую неделю откатили после расчёта: сумма долга в строках уже неверна."""
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        debtor = await make_cook(session, "Должник")
        first = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, debtor, first.start_date, "penalty", "900")
        first_run = await close_week(session, first, actor)
        second = await week_with_colleague(session, 1, colleague)
        await add_shift(session, debtor, second.start_date)
        second_run = await calculate(session, second)
        await unfinalize_payroll_run(
            session, first_run.id, reason="Правка штрафа", actor_user_id=actor.id
        )

        assert (await get_run(session, second_run.id))["needs_recalc"] is True
        with pytest.raises(PayrollConflictError, match="долг с прошлых недель изменился"):
            await finalize_payroll_run(session, second_run.id, finalized_by_user_id=actor.id)

        # Пересчёт после отката снимает отказ: долга на входе больше нет.
        run = await close_week(session, second, actor)
        assert (await only_line(session, run, debtor)).penalty_debt_in == ZERO


async def test_closing_is_refused_when_a_later_week_is_already_closed(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    """Долг читается из самой поздней закрытой недели: за ней он потерялся бы молча."""
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        debtor = await make_cook(session, "Должник")
        first = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, debtor, first.start_date, "penalty", "900")
        first_run = await calculate(session, first)
        second = await week_with_colleague(session, 1, colleague)
        await add_shift(session, debtor, second.start_date)
        second_run = await close_week(session, second, actor)

        with pytest.raises(PayrollConflictError, match="некуда перенести"):
            await finalize_payroll_run(session, first_run.id, finalized_by_user_id=actor.id)

        # Порядок восстановлен: следующая откатывается, закрывается первая, долг доходит.
        await unfinalize_payroll_run(
            session, second_run.id, reason="Закрыть по порядку", actor_user_id=actor.id
        )
        await finalize_payroll_run(session, first_run.id, finalized_by_user_id=actor.id)
        recounted = await close_week(session, second, actor)
        assert (await only_line(session, recounted, debtor)).penalty_debt_in == Decimal("900.00")


# --- депозит -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("debt", "deposit", "payable", "debt_out"),
    [
        # Без долга: обычные 1000 депозита категории 2.
        ("0", "1000.00", "1400.00", "0.00"),
        # После долга осталось 400: депозит удерживается только с них.
        ("2000", "400.00", "0.00", "0.00"),
        # Долг съел всё: депозита нет, остаток идёт дальше.
        ("5000", "0.00", "0.00", "2600.00"),
    ],
)
async def test_debt_reduces_the_deposit_base(
    async_session_factory: async_sessionmaker[AsyncSession],
    stand: None,
    debt: str,
    deposit: str,
    payable: str,
    debt_out: str,
) -> None:
    """В Тепле штраф идёт раньше депозита, и долг уменьшает базу так же, как штраф."""
    async with async_session_factory() as session:
        await enable_deposit_withholding(session)
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        debtor = await make_cook(session, "Должник")
        first = await week_with_colleague(session, 0, colleague)
        if Decimal(debt) > 0:
            await add_adjustment(session, debtor, first.start_date, "penalty", debt)
        await close_week(session, first, actor)
        second = await week_with_colleague(session, 1, colleague)
        await add_shift(session, debtor, second.start_date)

        run = await calculate(session, second)

        line = await only_line(session, run, debtor)
        assert line.deduction == Decimal(deposit)
        assert Decimal(str(line.components["deposit_withholding"])) == Decimal(deposit)
        assert line.total_payable == Decimal(payable)
        assert line.penalty_debt_out == Decimal(debt_out)
        assert_week_equality([line])
        withheld = await session.scalars(
            select(DepositTransaction.amount).where(
                DepositTransaction.run_id == run.id,
                DepositTransaction.employee_id == debtor.id,
                DepositTransaction.transaction_type == "accrual",
            )
        )
        assert sorted(withheld.all()) == ([Decimal(deposit)] if Decimal(deposit) > 0 else [])


async def test_penalty_of_the_week_also_comes_before_the_deposit(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    async with async_session_factory() as session:
        await enable_deposit_withholding(session)
        colleague = await make_cook(session, "Коллега")
        employee = await make_cook(session, "Смена и штраф")
        week = await week_with_colleague(session, 0, colleague)
        await add_shift(session, employee, week.start_date)
        await add_adjustment(session, employee, week.start_date, "penalty", "2000")

        run = await calculate(session, week)

        line = await only_line(session, run, employee)
        assert line.deduction == Decimal("2400.00")
        assert line.components["deposit_withholding"] == 400
        assert line.total_payable == ZERO
        assert_week_equality([line])


async def enable_scheduled_deposit_payout(session: AsyncSession) -> None:
    """Включить отложенную выдачу депозита: в базе стенда ключа нет."""
    session.add(
        AppSetting(
            id=uuid.uuid4(),
            key=DEPOSIT_SCHEDULED_PAYOUT_ENABLED_KEY,
            value=True,
            value_type="bool",
            category="payroll",
            display_name="Отложенная выдача депозита",
            widget_type="switch",
        )
    )
    await session.commit()


async def test_deposit_payout_line_carries_the_debt_forward(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    """Депозитная строка становится самой поздней: ноль на ней стёр бы долг."""
    async with async_session_factory() as session:
        await enable_scheduled_deposit_payout(session)
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        debtor = await make_cook(session, "Должник", status="dismissing")
        session.add(
            DepositAccount(
                id=uuid.uuid4(),
                employee_id=debtor.id,
                balance=Decimal("3000"),
                initial_balance=Decimal("3000"),
                last_updated=datetime(2026, 7, 1, tzinfo=UTC),
            )
        )
        first = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, debtor, first.start_date, "penalty", "900")
        await close_week(session, first, actor)
        second = await week_with_colleague(session, 1, colleague)
        session.add(
            DepositPayoutSchedule(
                id=uuid.uuid4(),
                employee_id=debtor.id,
                target_period_id=second.id,
                requested_amount=Decimal("3000"),
                status="pending",
            )
        )
        await session.commit()

        run = await calculate(session, second)

        line = await only_line(session, run, debtor)
        assert line.deposit_payout_scheduled == Decimal("3000.00")
        assert line.total_payable == ZERO
        assert line.penalty_debt_in == Decimal("900.00")
        assert line.penalty_debt_out == Decimal("900.00")


async def test_deposit_payout_sits_on_a_line_that_already_exists_without_shifts(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    """Строка из одного штрафа не отнимает у человека запланированную выдачу депозита."""
    async with async_session_factory() as session:
        await enable_scheduled_deposit_payout(session)
        colleague = await make_cook(session, "Коллега")
        employee = await make_cook(session, "Увольняется", status="dismissing")
        session.add(
            DepositAccount(
                id=uuid.uuid4(),
                employee_id=employee.id,
                balance=Decimal("3000"),
                initial_balance=Decimal("3000"),
                last_updated=datetime(2026, 7, 1, tzinfo=UTC),
            )
        )
        week = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, employee, week.start_date, "penalty", "700")
        session.add(
            DepositPayoutSchedule(
                id=uuid.uuid4(),
                employee_id=employee.id,
                target_period_id=week.id,
                requested_amount=Decimal("3000"),
                status="pending",
            )
        )
        await session.commit()

        run = await calculate(session, week)

        line = await only_line(session, run, employee)
        assert line.deposit_payout_scheduled == Decimal("3000.00")
        assert line.components["deposit_payout"] == 3000
        assert line.penalty_debt_out == Decimal("700.00")


async def test_penalty_of_a_person_with_a_vacation_lump_is_not_lost(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    """Транш отпускных заводит строку раньше штрафов: штраф садится на неё и уходит в долг."""
    async with async_session_factory() as session:
        colleague = await make_cook(session, "Коллега")
        employee = await make_cook(session, "В отпуске")
        week = await week_with_colleague(session, 0, colleague)
        session.add(
            VacationPeriod(
                id=uuid.uuid4(),
                employee_id=employee.id,
                date_start=week.start_date,
                date_end=week.start_date + timedelta(days=1),
                days_count=2,
                payout_date=week.payroll_date,
                status="planned",
            )
        )
        await session.commit()
        run = await calculate(session, week)
        lump = (await only_line(session, run, employee)).vacation_pay
        assert lump > 0
        await add_adjustment(
            session, employee, week.start_date, "penalty", str(lump + Decimal("700"))
        )

        run = await calculate(session, week)

        line = await only_line(session, run, employee)
        assert line.vacation_pay == lump
        assert line.total_payable == ZERO
        assert line.penalty_debt_out == Decimal("700.00")
        assert_week_equality([line])


# --- срез на дату ------------------------------------------------------------------


async def test_balance_shows_the_debt_from_the_closing_day_as_a_receivable(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        debtor = await make_cook(session, "Должник")
        first = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, debtor, first.start_date, "penalty", "3000")
        await close_week(session, first, actor)
        await stamp_closing_day(session, first, date(2026, 7, 15))

        before = await build_staff_balance_as_of(session, as_of=date(2026, 7, 14))
        closed = await build_staff_balance_as_of(session, as_of=date(2026, 7, 15))

        row_before = balance_row(before, debtor)
        assert row_before is None or row_before.penalty_debt == ZERO
        row = balance_row(closed, debtor)
        assert row is not None
        assert row.penalty_debt == Decimal("3000.00")
        assert row.receivable == Decimal("3000.00")
        assert closed.penalty_debt_total == Decimal("3000.00")
        assert closed.receivable_total == Decimal("3000.00")


async def test_balance_follows_the_debt_down_the_chain_by_closing_day(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        debtor = await make_cook(session, "Должник")
        first = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, debtor, first.start_date, "penalty", "3000")
        await close_week(session, first, actor)
        await stamp_closing_day(session, first, date(2026, 7, 15))
        second = await week_with_colleague(session, 1, colleague)
        await add_shift(session, debtor, second.start_date)
        await close_week(session, second, actor)
        await stamp_closing_day(session, second, date(2026, 7, 22))
        third = await week_with_colleague(session, 2, colleague)
        await add_shift(session, debtor, third.start_date)
        await close_week(session, third, actor)
        await stamp_closing_day(session, third, date(2026, 7, 29))

        expected = {
            date(2026, 7, 21): Decimal("3000.00"),
            date(2026, 7, 22): Decimal("600.00"),
            date(2026, 7, 28): Decimal("600.00"),
            date(2026, 7, 29): ZERO,
        }
        for as_of, debt in expected.items():
            report = await build_staff_balance_as_of(session, as_of=as_of)
            row = balance_row(report, debtor)
            assert (row.penalty_debt if row is not None else ZERO) == debt, as_of
            assert report.penalty_debt_total == debt


async def test_balance_counts_a_two_line_debt_once(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        two_roles = await make_cook(session, "Две роли", roles=("sushi", "pizza"))
        week = await week_with_colleague(session, 0, colleague)
        await add_shift(session, two_roles, week.start_date, role="sushi")
        await add_shift(session, two_roles, week.start_date + timedelta(days=1), role="pizza")
        await add_adjustment(session, two_roles, week.start_date, "penalty", "5000")
        await close_week(session, week, actor)
        await stamp_closing_day(session, week, date(2026, 7, 15))

        report = await build_staff_balance_as_of(session, as_of=date(2026, 7, 15))

        row = balance_row(report, two_roles)
        assert row is not None
        assert row.penalty_debt == Decimal("400.00")


async def test_balance_does_not_count_the_debt_of_a_dismissed_person(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    """Неделя уволенного показывается, долгом не ставится; статус истории не хранит."""
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        debtor = await make_cook(session, "Должник")
        first = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, debtor, first.start_date, "penalty", "900")
        await close_week(session, first, actor)
        await stamp_closing_day(session, first, date(2026, 7, 15))
        debtor.status = "inactive"
        debtor.fire_date = first.end_date
        await session.commit()

        report = await build_staff_balance_as_of(session, as_of=date(2026, 7, 15))

        row = balance_row(report, debtor)
        assert row is None or (row.penalty_debt == ZERO and row.receivable == ZERO)
        assert report.penalty_debt_total == ZERO


# --- выплата нулевой строки --------------------------------------------------------


async def zero_line_week(
    session: AsyncSession, actor: User
) -> tuple[PayrollPeriod, PayrollRun, Employee, Employee]:
    """Закрытая неделя: коллеге к выплате смена, должнику ноль, в Сейфе резерв под коллегу."""
    colleague = await make_cook(session, "Коллега")
    debtor = await make_cook(session, "Должник")
    week = await week_with_colleague(session, 0, colleague)
    await add_adjustment(session, debtor, week.start_date, "penalty", "900")
    run = await close_week(session, week, actor)
    wallet = await _fund_wallet(session, "cash_safe", Decimal("10000"))
    await _reserve_payroll(session, run_id=run.id, wallet=wallet, amount=SUSHI_SHIFT)
    return week, run, colleague, debtor


async def assert_closed_without_money(
    session: AsyncSession, run: PayrollRun, debtor: Employee
) -> None:
    """Нулевая строка отмечена оплаченной, а расхода ДДС на неё нет."""
    payment = await session.scalar(
        select(PayrollPayment).where(
            PayrollPayment.run_id == run.id,
            PayrollPayment.employee_id == debtor.id,
        )
    )
    assert payment is not None
    assert (payment.status, payment.amount) == ("paid", ZERO)
    bookings = await session.scalars(
        select(PayrollPayoutBooking).where(
            PayrollPayoutBooking.run_id == run.id,
            PayrollPayoutBooking.employee_id == debtor.id,
        )
    )
    assert bookings.all() == []


async def test_bulk_payment_of_the_whole_week_closes_a_zero_line_without_money(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    async with async_session_factory() as session:
        actor = await make_actor(session)
        week, run, _colleague, debtor = await zero_line_week(session, actor)

        marked = await mark_all_payments(
            session,
            run.id,
            paid_at=week.payroll_date,
            method="cash",
            actor_user_id=actor.id,
        )

        assert marked == 2
        await assert_closed_without_money(session, run, debtor)


async def test_selected_payment_of_only_a_zero_line_needs_no_reserve(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    """Денег нет, и резерв под них не нужен: «Выплатить» по одной нулевой строке проходит."""
    async with async_session_factory() as session:
        actor = await make_actor(session)
        week, run, _colleague, debtor = await zero_line_week(session, actor)

        marked = await mark_payments_selected(
            session,
            run.id,
            [debtor.id],
            paid_at=week.payroll_date,
            actor_user_id=actor.id,
        )

        assert marked == 1
        await assert_closed_without_money(session, run, debtor)


# --- персональный отчёт и дверь ----------------------------------------------------


async def test_personal_report_shows_the_week_without_shifts_and_the_debt(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        debtor = await make_cook(session, "Должник")
        first = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, debtor, first.start_date, "penalty", "3000")
        await close_week(session, first, actor)
        second = await week_with_colleague(session, 1, colleague)
        await add_shift(session, debtor, second.start_date)
        await close_week(session, second, actor)

        report = await build_personal_report(session, debtor.id, first.start_date, second.end_date)

        periods = {period["period_start"]: period for period in report["periods"]}
        assert set(periods) == {first.start_date, second.start_date}
        without_shifts = periods[first.start_date]
        assert without_shifts["days"] == []
        assert without_shifts["penalty_total"] == 3000
        assert without_shifts["total_payable"] == 0
        assert without_shifts["penalty_debt_out"] == 3000
        assert without_shifts["roles"][0]["penalty_debt_out"] == 3000
        withheld = periods[second.start_date]
        assert withheld["penalty_debt_in"] == 3000
        assert withheld["penalty_debt_out"] == 600
        PayrollPersonalReportPeriodRead.model_validate(
            {**without_shifts, "manual_deposit_payout": 0, "manual_deposit_transactions": []}
        )
        # Итог периода долг не складывает: один и тот же долг стоял бы в каждой неделе.
        assert not {"penalty_debt_in", "penalty_debt_out"} & set(report["totals"])


async def test_personal_report_gives_the_written_off_amount(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    async with async_session_factory() as session:
        actor = await make_actor(session)
        colleague = await make_cook(session, "Коллега")
        dismissed = await make_cook(
            session, "Уволенный", status="inactive", fire_date=date(2026, 7, 1)
        )
        week = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, dismissed, week.start_date, "penalty", "839.42")
        await close_week(session, week, actor)

        report = await build_personal_report(session, dismissed.id, week.start_date, week.end_date)

        assert report["periods"][0]["penalty_debt_written_off"] == 839.42
        assert report["periods"][0]["penalty_debt_out"] == 0


async def test_payroll_line_door_gives_the_debt_as_numbers(
    async_session_factory: async_sessionmaker[AsyncSession], stand: None
) -> None:
    async with async_session_factory() as session:
        colleague = await make_cook(session, "Коллега")
        debtor = await make_cook(session, "Должник")
        week = await week_with_colleague(session, 0, colleague)
        await add_adjustment(session, debtor, week.start_date, "penalty", "839.42")
        run = await calculate(session, week)

        read = serialize_payroll_line(await only_line(session, run, debtor), {})

        assert read.penalty_debt_in == 0
        assert read.penalty_debt_out == 839.42
        assert read.penalty_debt_written_off == 0

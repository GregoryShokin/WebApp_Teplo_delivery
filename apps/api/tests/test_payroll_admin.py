from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    DishwasherShift,
    Employee,
    EmployeePositionAssignment,
    PayrollAdjustment,
    PayrollLine,
    PayrollPeriod,
    PayrollRate,
    PayrollRun,
)
from app.services.payroll_admin import (
    latest_admin_period_dates,
    next_admin_period_dates,
    run_admin_payroll,
    set_admin_payroll_exclusion,
    set_dishwasher_shift,
    set_okladnik_payout_mode,
)
from app.services.payroll_runner import PayrollConflictError

FIRST_HALF = (date(2026, 5, 1), date(2026, 5, 15), date(2026, 5, 15))


# --------------------------------------------------------------------------- #
# Чистые помощники полумесячной математики
# --------------------------------------------------------------------------- #
def test_latest_admin_period_after_15th_is_first_half() -> None:
    start, end, payday = latest_admin_period_dates(date(2026, 5, 20))
    assert (start, end, payday) == (date(2026, 5, 1), date(2026, 5, 15), date(2026, 5, 15))


def test_latest_admin_period_before_15th_is_prev_second_half() -> None:
    start, end, payday = latest_admin_period_dates(date(2026, 5, 3))
    assert (start, end, payday) == (date(2026, 4, 16), date(2026, 4, 30), date(2026, 5, 1))


def test_next_admin_period_first_to_second_half() -> None:
    start, end, payday = next_admin_period_dates(date(2026, 5, 1), date(2026, 5, 15))
    assert (start, end, payday) == (date(2026, 5, 16), date(2026, 5, 31), date(2026, 6, 1))


def test_next_admin_period_second_half_rolls_to_next_month() -> None:
    start, end, payday = next_admin_period_dates(date(2026, 5, 16), date(2026, 5, 31))
    assert (start, end, payday) == (date(2026, 6, 1), date(2026, 6, 15), date(2026, 6, 15))


def test_next_admin_period_december_rolls_year() -> None:
    start, end, payday = next_admin_period_dates(date(2026, 12, 16), date(2026, 12, 31))
    assert (start, end, payday) == (date(2027, 1, 1), date(2027, 1, 15), date(2027, 1, 15))


# --------------------------------------------------------------------------- #
# Интеграционные тесты расчёта
# --------------------------------------------------------------------------- #
async def _make_admin_employee(
    session: AsyncSession,
    *,
    position: str = "Управляющий",
    hire_date: date | None = None,
    fire_date: date | None = None,
) -> Employee:
    employee = Employee(
        id=uuid.uuid4(),
        full_name="Админ Сотрудник",
        iiko_id=f"iiko-{uuid.uuid4()}",
        status="active",
        is_senior=False,
        is_deputy_senior=False,
        hire_date=hire_date,
        fire_date=fire_date,
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
            position=position,
            effective_from=date(2026, 1, 1),
            effective_to=None,
        )
    )
    await session.flush()
    return employee


async def _set_oklad(
    session: AsyncSession,
    *,
    position: str,
    amount: Decimal,
    employee_id: uuid.UUID | None = None,
) -> None:
    session.add(
        PayrollRate(
            id=uuid.uuid4(),
            employee_id=employee_id,
            position_group=position,
            category="admin",
            station=None,
            rate_type="monthly",
            amount=amount,
            is_active=True,
            effective_from=date(2026, 1, 1),
        )
    )
    await session.flush()


async def _make_period(session: AsyncSession) -> PayrollPeriod:
    start, end, payday = FIRST_HALF
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="half_month",
        start_date=start,
        end_date=end,
        payroll_date=payday,
        status="open",
    )
    session.add(period)
    await session.flush()
    return period


async def _lines(session: AsyncSession, run_id: uuid.UUID) -> list[PayrollLine]:
    result = await session.scalars(select(PayrollLine).where(PayrollLine.run_id == run_id))
    return list(result.all())


async def test_admin_run_full_period_is_half_oklad(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _make_admin_employee(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("60000"))
        period = await _make_period(session)
        await session.commit()

        run = await run_admin_payroll(session, period.id)
        assert run.status == "completed"
        lines = await _lines(session, run.id)
        assert len(lines) == 1
        line = lines[0]
        assert line.employee_id == employee.id
        assert line.role == "Управляющий"
        assert line.base_pay == Decimal("30000.00")
        assert line.total_payable == Decimal("30000.00")
        assert line.ndfl_withheld == Decimal("0")


async def test_senior_courier_gets_admin_oklad(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Старший курьер (archetype=courier) получает админ-оклад в полумесячной ведомости."""
    async with async_session_factory() as session:
        employee = await _make_admin_employee(session, position="Старший курьер")
        await _set_oklad(session, position="Старший курьер", amount=Decimal("40000"))
        period = await _make_period(session)
        await session.commit()

        run = await run_admin_payroll(session, period.id)
        assert run.status == "completed"
        lines = await _lines(session, run.id)
        assert len(lines) == 1
        line = lines[0]
        assert line.employee_id == employee.id
        assert line.role == "Старший курьер"
        # Режим по умолчанию — split (½ за первую половину).
        assert line.base_pay == Decimal("20000.00")
        assert line.total_payable == Decimal("20000.00")


async def test_admin_salaries_response_exposes_payout_mode(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Режим выплаты не должен срезаться response-схемой (регрессия UI «остаётся Пополам»)."""
    from app.api.v1.routes.payroll_admin import AdminSalariesRead
    from app.services.payroll_admin import list_admin_oklady, set_okladnik_payout_mode

    async with async_session_factory() as session:
        await _make_admin_employee(session, position="Уборщица")
        await _set_oklad(session, position="Уборщица", amount=Decimal("15000"))
        await set_okladnik_payout_mode(session, "Уборщица", "first_half")
        await session.commit()

        data = await list_admin_oklady(session)
        serialized = AdminSalariesRead.model_validate(data).model_dump()
        by_position = {item["position"]: item for item in serialized["defaults"]}
        assert by_position["Уборщица"]["payout_mode"] == "first_half"


async def test_admin_run_prorates_on_mid_period_hire(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        # Нанят 8 мая внутри периода 1–15 (15 дней) → отработано 8 дней (8–15).
        await _make_admin_employee(session, hire_date=date(2026, 5, 8))
        await _set_oklad(session, position="Управляющий", amount=Decimal("60000"))
        period = await _make_period(session)
        await session.commit()

        run = await run_admin_payroll(session, period.id)
        lines = await _lines(session, run.id)
        assert len(lines) == 1
        # 30000 * 8/15 = 16000.00
        assert lines[0].base_pay == Decimal("16000.00")
        assert lines[0].total_payable == Decimal("16000.00")


async def test_admin_run_includes_admin_adjustments_but_not_production(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _make_admin_employee(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("60000"))
        period = await _make_period(session)
        # Премия как управляющему (попадает) и штраф как повару-субституту (не попадает).
        session.add(
            PayrollAdjustment(
                id=uuid.uuid4(),
                employee_id=employee.id,
                work_date=date(2026, 5, 10),
                type="bonus",
                role="Управляющий",
                custom_label="Премия за месяц",
                amount=Decimal("5000"),
            )
        )
        session.add(
            PayrollAdjustment(
                id=uuid.uuid4(),
                employee_id=employee.id,
                work_date=date(2026, 5, 10),
                type="penalty",
                role="Повар",
                custom_label="Штраф за смену повара",
                amount=Decimal("1000"),
            )
        )
        await session.commit()

        run = await run_admin_payroll(session, period.id)
        line = (await _lines(session, run.id))[0]
        assert line.premium == Decimal("5000.00")
        assert line.deduction == Decimal("0.00")  # производственный штраф не учтён
        assert line.total_payable == Decimal("35000.00")


async def test_admin_oklad_employee_override_wins(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _make_admin_employee(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("60000"))
        await _set_oklad(
            session, position="Управляющий", amount=Decimal("80000"), employee_id=employee.id
        )
        period = await _make_period(session)
        await session.commit()

        run = await run_admin_payroll(session, period.id)
        line = (await _lines(session, run.id))[0]
        assert line.base_pay == Decimal("40000.00")  # 80000/2 — переопределение, не дефолт


async def test_admin_run_skips_employee_without_oklad(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _make_admin_employee(session)
        period = await _make_period(session)
        await session.commit()

        # Нет оклада — сотрудник не попадает в ведомость, но это НЕ блокирует расчёт.
        run = await run_admin_payroll(session, period.id)
        assert run.status == "completed"
        assert not run.blocking_issues
        assert await _lines(session, run.id) == []
        excluded = run.summary["excluded_no_oklad"]
        assert [item["employee_id"] for item in excluded] == [str(employee.id)]


async def _make_second_half_period(session: AsyncSession) -> PayrollPeriod:
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="half_month",
        start_date=date(2026, 5, 16),
        end_date=date(2026, 5, 31),
        payroll_date=date(2026, 6, 1),
        status="open",
    )
    session.add(period)
    await session.flush()
    return period


async def test_cleaner_first_half_mode_pays_full_oklad_on_15th(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _make_admin_employee(session, position="Уборщица")
        await _set_oklad(session, position="Уборщица", amount=Decimal("15000"))
        await set_okladnik_payout_mode(session, "Уборщица", "first_half")
        period = await _make_period(session)  # первая половина (1–15)
        await session.commit()

        run = await run_admin_payroll(session, period.id)
        line = (await _lines(session, run.id))[0]
        # Режим «всё на 15-е»: за первую половину — весь оклад, а не половина.
        assert line.base_pay == Decimal("15000.00")
        assert line.total_payable == Decimal("15000.00")


async def test_cleaner_first_half_mode_skips_second_half(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _make_admin_employee(session, position="Уборщица")
        await _set_oklad(session, position="Уборщица", amount=Decimal("15000"))
        await set_okladnik_payout_mode(session, "Уборщица", "first_half")
        period = await _make_second_half_period(session)
        await session.commit()

        run = await run_admin_payroll(session, period.id)
        # Во второй половине при режиме «всё на 15-е» уборщице ничего не начисляется.
        assert await _lines(session, run.id) == []


async def test_dishwasher_paid_by_shifts_from_pool(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _make_admin_employee(session, position="Посудомойка")
        period = await _make_period(session)  # 1–15 мая, месяц = 31 день
        for day in range(1, 11):  # 10 смен в полупериоде
            session.add(
                DishwasherShift(
                    id=uuid.uuid4(), employee_id=employee.id, work_date=date(2026, 5, day)
                )
            )
        await session.commit()

        run = await run_admin_payroll(session, period.id)
        line = (await _lines(session, run.id))[0]
        assert line.role == "Посудомойка"
        # Ставка = 15000 / 31 = 483.87; 10 смен → 4838.70
        assert line.base_pay == Decimal("4838.70")
        assert line.total_payable == Decimal("4838.70")


async def test_dishwasher_without_shifts_not_in_run(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _make_admin_employee(session, position="Посудомойка")
        period = await _make_period(session)
        await session.commit()

        run = await run_admin_payroll(session, period.id)
        assert await _lines(session, run.id) == []


async def _add_finalized_run(
    session: AsyncSession,
    *,
    period_type: str,
    start: date,
    end: date,
) -> None:
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type=period_type,
        start_date=start,
        end_date=end,
        payroll_date=end,
        status="finalized",
    )
    session.add(period)
    await session.flush()
    session.add(
        PayrollRun(
            id=uuid.uuid4(),
            period_id=period.id,
            status="finalized",
            blocking_issues=[],
            summary={},
        )
    )
    await session.flush()


async def test_dishwasher_shift_not_blocked_by_production_finalization(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _make_admin_employee(session, position="Посудомойка")
        # Финализированная НЕДЕЛЬНАЯ производственная ведомость не блокирует смены мойщиц.
        await _add_finalized_run(
            session, period_type="week", start=date(2026, 5, 4), end=date(2026, 5, 10)
        )
        await session.commit()

        await set_dishwasher_shift(
            session, employee_id=employee.id, work_date=date(2026, 5, 5), worked=True
        )
        count = await session.scalar(
            select(func.count())
            .select_from(DishwasherShift)
            .where(DishwasherShift.employee_id == employee.id)
        )
        assert count == 1


async def test_dishwasher_shift_blocked_by_admin_finalization(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _make_admin_employee(session, position="Посудомойка")
        await _add_finalized_run(
            session, period_type="half_month", start=date(2026, 5, 1), end=date(2026, 5, 15)
        )
        await session.commit()

        with pytest.raises(PayrollConflictError):
            await set_dishwasher_shift(
                session, employee_id=employee.id, work_date=date(2026, 5, 5), worked=True
            )


async def test_admin_run_respects_exclusion_toggle(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _make_admin_employee(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("60000"))
        period = await _make_period(session)
        await session.commit()

        # Оклад есть, но сотрудник исключён вручную — в ведомость не попадает и
        # в excluded_no_oklad его тоже нет (исключение намеренное, не из-за оклада).
        await set_admin_payroll_exclusion(session, employee.id, True)
        run = await run_admin_payroll(session, period.id)
        assert run.status == "completed"
        assert await _lines(session, run.id) == []
        assert run.summary["excluded_no_oklad"] == []


async def test_admin_half_month_finalization_does_not_lock_shift_ledger(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Регрессия: финализация админской полумесячной ведомости (1–15, выплата 15-го) НЕ
    # должна морозить ячейки производственного Учёта смен за ещё не закрытую неделю 9–15.
    # Раньше водяная отметка блокировки бралась по всем типам периодов, и end_date
    # админского периода (15 июня) морозил весь табель первой половины месяца.
    from app.services import shift_ledger as shift_ledger_service
    from app.services.shift_ledger import (
        get_latest_locked_payroll_date,
        is_payroll_locked,
    )

    monkeypatch.setattr(shift_ledger_service, "ledger_today", lambda: date(2026, 6, 16))

    async with async_session_factory() as session:
        # Последняя финализированная производственная неделя — по понедельник 8 июня.
        await _add_finalized_run(
            session, period_type="week", start=date(2026, 6, 2), end=date(2026, 6, 8)
        )
        # Админская полумесячная 1–15 финализирована — её end_date в блокировке табеля не учитываем.
        await _add_finalized_run(
            session, period_type="half_month", start=date(2026, 6, 1), end=date(2026, 6, 15)
        )
        await session.commit()

        latest = await get_latest_locked_payroll_date(session)
        # Отметка — по производственной неделе (8 июня), а не по админскому периоду (15 июня).
        assert latest == date(2026, 6, 8)
        # Дни ещё не закрытой производственной недели 9–15 редактируемы.
        assert is_payroll_locked(date(2026, 6, 9), latest) is False
        assert is_payroll_locked(date(2026, 6, 15), latest) is False
        # Дни закрытой производственной недели остаются заблокированными.
        assert is_payroll_locked(date(2026, 6, 8), latest) is True

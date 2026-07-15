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
    EmployeePayout,
    EmployeePositionAssignment,
    PayrollAdjustment,
    PayrollLine,
    PayrollPeriod,
    PayrollRate,
    PayrollRun,
)
from app.services.payroll_admin import (
    compute_on_demand_debt,
    create_included_payout,
    latest_admin_period_dates,
    next_admin_period_dates,
    okladnik_earned_to_date,
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
    effective_from: date = date(2026, 1, 1),
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
            effective_from=effective_from,
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


async def test_on_demand_mode_accrues_debt_not_paid(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Режим on_demand: оклад начисляется в долг (components.proration.accrual_amount),
    но к автовыплате база = 0 — собственник получает по востребованию."""
    async with async_session_factory() as session:
        await _make_admin_employee(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("60000"))
        await set_okladnik_payout_mode(session, "Управляющий", "on_demand")
        period = await _make_period(session)
        await session.commit()

        run = await run_admin_payroll(session, period.id)
        assert run.status == "completed"
        lines = await _lines(session, run.id)
        assert len(lines) == 1
        line = lines[0]
        assert line.base_pay == Decimal("0.00")
        assert line.total_payable == Decimal("0.00")
        proration = line.components["proration"]
        assert proration["on_demand"] is True
        # Полный месячный оклад начисляется сразу (в первом полупериоде), не ½.
        assert Decimal(proration["accrual_amount"]) == Decimal("60000.00")


async def test_on_demand_debt_accrued_minus_payouts(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Долг ЗП собственника = Σ начислено (on_demand-строки) − Σ выплат EmployeePayout."""
    async with async_session_factory() as session:
        employee = await _make_admin_employee(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("60000"))
        await set_okladnik_payout_mode(session, "Управляющий", "on_demand")
        period = await _make_period(session)
        await session.commit()

        await run_admin_payroll(session, period.id)

        # Начислено 60000 (полный оклад), ничего не выплачено → остаток 60000.
        debt = await compute_on_demand_debt(session, [employee.id])
        assert debt[employee.id]["accrued"] == Decimal("60000.00")
        assert debt[employee.id]["paid"] == Decimal("0.00")
        assert debt[employee.id]["debt"] == Decimal("60000.00")

        # Выплата 10000 (paid) уменьшает остаток до 50000; черновик (draft) не учитывается.
        session.add(
            EmployeePayout(
                id=uuid.uuid4(),
                employee_id=employee.id,
                kind="owner_salary",
                amount=Decimal("10000.00"),
                payout_date=date(2026, 5, 20),
                status="paid",
            )
        )
        session.add(
            EmployeePayout(
                id=uuid.uuid4(),
                employee_id=employee.id,
                kind="owner_salary",
                amount=Decimal("5000.00"),
                payout_date=date(2026, 5, 21),
                status="draft",
            )
        )
        await session.commit()

        debt = await compute_on_demand_debt(session, [employee.id])
        assert debt[employee.id]["paid"] == Decimal("10000.00")
        assert debt[employee.id]["debt"] == Decimal("50000.00")


async def test_on_demand_premium_not_auto_paid(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """on_demand: премия на строке НЕ уходит в авто-выплату ведомости (к выплате 0).

    Остаток = полный оклад (корректировки в остаток не складываются — редкий случай для
    собственника; премия видна в своей колонке, но по ведомости не платится и в остаток не идёт)."""
    async with async_session_factory() as session:
        employee = await _make_admin_employee(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("60000"))
        await set_okladnik_payout_mode(session, "Управляющий", "on_demand")
        period = await _make_period(session)
        session.add(
            PayrollAdjustment(
                id=uuid.uuid4(),
                employee_id=employee.id,
                work_date=date(2026, 5, 10),
                type="bonus",
                role="Управляющий",
                custom_label="Премия",
                amount=Decimal("5000"),
            )
        )
        await session.commit()

        run = await run_admin_payroll(session, period.id)
        assert run.status == "completed"
        lines = await _lines(session, run.id)
        assert len(lines) == 1
        line = lines[0]
        assert line.premium == Decimal("5000.00")
        assert line.total_payable == Decimal("0.00")
        # Остаток = полный оклад (без премии).
        assert Decimal(line.components["proration"]["accrual_amount"]) == Decimal("60000.00")
        assert float(run.summary["total_payable"]) == 0.0

        debt = await compute_on_demand_debt(session, [employee.id])
        assert debt[employee.id]["debt"] == Decimal("60000.00")


async def test_on_demand_include_bumps_payable_and_reduces_remaining(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Включить в выплату»: сумма попадает в total_payable строки и уменьшает остаток;
    переживает пересчёт."""
    async with async_session_factory() as session:
        employee = await _make_admin_employee(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("60000"))
        await set_okladnik_payout_mode(session, "Управляющий", "on_demand")
        period = await _make_period(session)
        await session.commit()

        run = await run_admin_payroll(session, period.id)
        lines = await _lines(session, run.id)
        assert lines[0].total_payable == Decimal("0.00")

        # Включаем 25000 в выплату ведомости.
        await create_included_payout(
            session, run_id=run.id, employee_id=employee.id, amount=Decimal("25000")
        )
        await session.commit()

        lines = await _lines(session, run.id)
        assert lines[0].total_payable == Decimal("25000.00")
        debt = await compute_on_demand_debt(session, [employee.id])
        assert debt[employee.id]["paid"] == Decimal("25000.00")
        assert debt[employee.id]["debt"] == Decimal("35000.00")

        # Пересчёт ведомости: включённая сумма сохраняется в total_payable.
        run2 = await run_admin_payroll(session, period.id)
        lines2 = await _lines(session, run2.id)
        assert lines2[0].total_payable == Decimal("25000.00")


async def test_on_demand_full_oklad_both_halves_deduped_per_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """on_demand: обе половины месяца показывают полный оклад, но в остатке он ОДИН раз/месяц."""
    async with async_session_factory() as session:
        employee = await _make_admin_employee(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("60000"))
        await set_okladnik_payout_mode(session, "Управляющий", "on_demand")
        first = await _make_period(session)
        second = await _make_second_half_period(session)
        await session.commit()

        run1 = await run_admin_payroll(session, first.id)
        run2 = await run_admin_payroll(session, second.id)

        # Обе половины: начислено = полный оклад.
        line1 = (await _lines(session, run1.id))[0]
        line2 = (await _lines(session, run2.id))[0]
        assert Decimal(line1.components["proration"]["accrual_amount"]) == Decimal("60000.00")
        assert Decimal(line2.components["proration"]["accrual_amount"]) == Decimal("60000.00")

        # Остаток: оклад считается один раз за месяц (60000, не 120000).
        debt = await compute_on_demand_debt(session, [employee.id])
        assert debt[employee.id]["accrued"] == Decimal("60000.00")
        assert debt[employee.id]["debt"] == Decimal("60000.00")


async def test_cashier_acting_as_assistant_manager_in_admin_run(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Кассир с персональным окладом «Помощник менеджера» идёт в админ-ведомость по этой
    должности (СВЕРХ своей производственной ЗП), хотя его основная должность — Кассир."""
    async with async_session_factory() as session:
        cashier = await _make_admin_employee(session, position="Кассир")
        # Персональный оклад помощника менеджера для кассира = назначение «исполняющего».
        await _set_oklad(
            session,
            position="Помощник менеджера",
            amount=Decimal("6000"),
            employee_id=cashier.id,
        )
        period = await _make_period(session)
        await session.commit()

        run = await run_admin_payroll(session, period.id)
        assert run.status == "completed"
        lines = await _lines(session, run.id)
        assert len(lines) == 1
        line = lines[0]
        assert line.employee_id == cashier.id
        # В админ-ведомости он идёт как «Помощник менеджера», не как «Кассир».
        assert line.role == "Помощник менеджера"
        # split по умолчанию → ½ от 6000 = 3000 за первую половину.
        assert line.base_pay == Decimal("3000.00")
        assert line.total_payable == Decimal("3000.00")


async def test_acting_assistant_effective_on_payout_date_enters_second_half_run(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Назначение «исполняющего» с датой = дата ВЫПЛАТЫ попадает во вторую половину месяца.

    Период 16–31 мая, выплата 1 июня. Оклад помощника менеджера действует с 1 июня (дата
    выплаты). По концу периода (31 мая) он бы НЕ применился — а по дате выплаты (1 июня)
    применяется, и кассир попадает в ведомость. Регресс на «сотрудник на 1-е число не
    подтягивается при пересчёте».
    """
    async with async_session_factory() as session:
        cashier = await _make_admin_employee(session, position="Кассир")
        await _set_oklad(
            session,
            position="Помощник менеджера",
            amount=Decimal("6000"),
            employee_id=cashier.id,
            effective_from=date(2026, 6, 1),  # = payroll_date второй половины мая
        )
        period = await _make_second_half_period(session)  # 16–31 мая, выплата 1 июня
        await session.commit()

        run = await run_admin_payroll(session, period.id)
        assert run.status == "completed"
        lines = await _lines(session, run.id)
        assert len(lines) == 1
        line = lines[0]
        assert line.employee_id == cashier.id
        assert line.role == "Помощник менеджера"
        # split по умолчанию → ½ от 6000 за вторую половину.
        assert line.base_pay == Decimal("3000.00")


async def test_assistant_manager_position_seeded_with_oklad(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Миграция 0156: «Помощник менеджера» — окладник-админ с дефолтным окладом 6000 ₽."""
    from app.services import position_registry
    from app.services.payroll_admin import list_admin_oklady

    async with async_session_factory() as session:
        await position_registry.refresh_position_registry(session)
        try:
            assert "Помощник менеджера" in position_registry.okladnik_positions()
            assert "Помощник менеджера" in position_registry.admin_payroll_positions()

            data = await list_admin_oklady(session, as_of=date(2026, 6, 1))
            row = next(
                d for d in data["defaults"] if d["position"] == "Помощник менеджера"
            )
            assert row["amount"] == 6000
        finally:
            position_registry.reset_position_registry_for_tests()


def test_okladnik_earned_to_date_on_demand_returns_accrual() -> None:
    """on_demand: «заработано на дату» = прорейт-начисление (в долг), а не 0 (payout base).

    Иначе потолок аванса «в пределах заработанного» для собственника схлопнулся бы в 0.
    """
    employee = Employee(hire_date=None, fire_date=None)
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="half_month",
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 15),
        payroll_date=date(2026, 5, 15),
        status="open",
    )
    earned = okladnik_earned_to_date(
        Decimal("60000"), "on_demand", employee, period, date(2026, 5, 15)
    )
    # Полный оклад начисляется в первом полупериоде.
    assert earned == Decimal("60000.00")


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


async def test_dishwasher_pool_is_split_equally_between_half_month_runs(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _make_admin_employee(session, position="Посудомойка")
        first = await _make_period(session)
        second = await _make_second_half_period(session)
        for day in range(1, 32):
            session.add(
                DishwasherShift(
                    id=uuid.uuid4(), employee_id=employee.id, work_date=date(2026, 5, day)
                )
            )
        await session.commit()

        first_run = await run_admin_payroll(session, first.id)
        second_run = await run_admin_payroll(session, second.id)
        first_line = (await _lines(session, first_run.id))[0]
        second_line = (await _lines(session, second_run.id))[0]

        assert first_line.base_pay == Decimal("7500.00")
        assert first_line.components["period_days"] == 15
        assert first_line.components["shift_rate"] == "500.00"
        assert second_line.base_pay == Decimal("7500.00")
        assert second_line.components["period_days"] == 16
        assert second_line.components["shift_rate"] == "468.75"


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


async def test_dishwasher_period_pool_is_distributed_by_employee_shifts(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employees = [await _make_admin_employee(session, position="Посудомойка") for _ in range(3)]
        period = await _make_period(session)
        next_day = 1
        for employee, shift_count in zip(employees, (7, 4, 4), strict=True):
            for day in range(next_day, next_day + shift_count):
                session.add(
                    DishwasherShift(
                        id=uuid.uuid4(), employee_id=employee.id, work_date=date(2026, 5, day)
                    )
                )
            next_day += shift_count
        await session.commit()

        run = await run_admin_payroll(session, period.id)
        lines = await _lines(session, run.id)
        by_employee = {line.employee_id: line for line in lines}

        # Половина пула на ведомость = 7500; 7500 / 15 дней = 500 за смену.
        assert [by_employee[employee.id].base_pay for employee in employees] == [
            Decimal("3500.00"),
            Decimal("2000.00"),
            Decimal("2000.00"),
        ]
        assert sum((line.total_payable for line in lines), Decimal("0")) == Decimal("7500.00")
        for line in lines:
            assert line.role == "Посудомойка"
            assert line.components["monthly_pool"] == "15000.00"
            assert line.components["period_pool"] == "7500.00"
            assert line.components["period_days"] == 15
            assert line.components["shift_rate"] == "500.00"


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


async def test_eligible_payout_dates_include_open_week_skip_finalized(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Даты выплаты отпускных = незакрытые ведомости. Последняя финализированная неделя —
    # выплата 9 июня (вторник); значит первая доступная дата — следующий вторник 16 июня
    # (его ведомость ещё не закрыта), а сама дата 9 июня не предлагается.
    from app.services.vacation_service import eligible_payout_dates

    async with async_session_factory() as session:
        session.add(
            PayrollPeriod(
                id=uuid.uuid4(),
                period_type="week",
                start_date=date(2026, 6, 2),
                end_date=date(2026, 6, 8),
                payroll_date=date(2026, 6, 9),
                status="finalized",
            )
        )
        await session.commit()

        dates = await eligible_payout_dates(session, count=4)
        assert dates == [
            date(2026, 6, 16),
            date(2026, 6, 23),
            date(2026, 6, 30),
            date(2026, 7, 7),
        ]
        assert date(2026, 6, 9) not in dates


async def test_adjustment_lock_is_role_aware(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Премия/штраф производственнику (повар/кассир) проверяется по недельной ведомости,
    # админу — по полумесячной. Финализация админской 1–15 НЕ должна блокировать повара
    # за 9 июня, чья недельная ведомость 9–15 ещё открыта (это исходная жалоба владельца).
    from app.services.payroll_adjustment_service import is_date_locked_for_role
    from app.services.position_registry import admin_payroll_positions

    admin_role = admin_payroll_positions()[0]

    async with async_session_factory() as session:
        # Админская полумесячная 1–15 июня финализирована.
        await _add_finalized_run(
            session, period_type="half_month", start=date(2026, 6, 1), end=date(2026, 6, 15)
        )
        # Производственная неделя 2–8 июня финализирована; неделя 9–15 — нет (её не создаём).
        await _add_finalized_run(
            session, period_type="week", start=date(2026, 6, 2), end=date(2026, 6, 8)
        )
        await session.commit()

        # Повар на 9 июня: недельная ведомость не закрыта → НЕ заблокировано,
        # хотя админская полумесячная 1–15 финализирована.
        assert await is_date_locked_for_role(session, date(2026, 6, 9), "Повар") is False
        # Та же дата для админ-роли заблокирована — её полумесячная ведомость закрыта.
        assert await is_date_locked_for_role(session, date(2026, 6, 9), admin_role) is True
        # Производственная неделя 2–8 закрыта → повар на 5 июня заблокирован.
        assert await is_date_locked_for_role(session, date(2026, 6, 5), "Повар") is True

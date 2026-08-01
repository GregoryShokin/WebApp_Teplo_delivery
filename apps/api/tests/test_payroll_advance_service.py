"""Интеграционные тесты выдачи авансов/займов и доступного (через БД).

Закрывает связку 2b (доступное, окладничья ветка) + 3a (выдача и классификация
аванс/заём). Производственная ветка доступного тестируется отдельно (нужны явки).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CashflowTransaction,
    DdsArticle,
    Employee,
    EmployeePositionAssignment,
    PayrollLine,
    PayrollPeriod,
    PayrollRate,
    Wallet,
)
from app.services import clock
from app.services.payroll_admin import run_admin_payroll
from app.services.payroll_advance_availability import available_to_advance
from app.services.payroll_advance_service import (
    cancel_advance,
    issue_advance,
    set_advance_recovery_deferral,
    set_advance_recovery_overrides,
    set_loan_max,
    write_off_advance,
)
from app.services.payroll_runner import (
    PayrollConflictError,
    finalize_payroll_run,
    unfinalize_payroll_run,
)

# Полупериод 1–15 мая (15 дней). На 5-е число прошло 5 дней.
AS_OF = date(2026, 5, 5)


async def _make_okladnik(session: AsyncSession, *, position: str = "Управляющий") -> Employee:
    employee = Employee(
        id=uuid.uuid4(),
        full_name="Окладник Тест",
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
            position=position,
            effective_from=date(2026, 1, 1),
            effective_to=None,
        )
    )
    await session.flush()
    return employee


async def _set_oklad(session: AsyncSession, *, position: str, amount: Decimal) -> None:
    session.add(
        PayrollRate(
            id=uuid.uuid4(),
            employee_id=None,
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


async def test_okladnik_availability_reflects_calendar_earning(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        await session.commit()

        avail = await available_to_advance(session, emp, AS_OF)
        # split → база полупериода 45000; на 5-е из 15 дней = 15000.
        assert avail.basis == "okladnik"
        assert avail.earned_to_date == Decimal("15000.00")
        assert avail.already_advanced == Decimal("0.00")
        assert avail.available == Decimal("15000.00")
        assert (avail.period_start, avail.period_end) == (date(2026, 5, 1), date(2026, 5, 15))


async def test_okladnik_second_half_payout_day_blocks(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """2-я половина: на 1-е число (день выплаты [16..конец] прошлого месяца) аванс = 0.

    Проверяет исправление «для 2-й половины отсечка не срабатывала никогда»: на 1-е
    `_half_month_bounds` вернул бы новый период, но день выплаты детектируется отдельно.
    """
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        await session.commit()

        avail = await available_to_advance(session, emp, date(2026, 6, 1))
        assert avail.basis == "okladnik"
        assert avail.available == Decimal("0.00")
        assert avail.payout_reached is True
        assert avail.note is not None
        # Заработанное показываем по ОПЛАЧИВАЕМОМУ периоду — 2-я половина МАЯ [16..31].
        assert (avail.period_start, avail.period_end) == (date(2026, 5, 16), date(2026, 5, 31))
        assert avail.earned_to_date == Decimal("45000.00")


async def test_issue_advance_payout_gate_present_day_only(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    """Отсечка блокирует выдачу аванса СЕГОДНЯшней датой в день выплаты, но НЕ блокирует
    запись задним числом (деньги уже ушли) и пропускает явный заём. «Сегодня» пиним на
    15 мая (день выплаты [1..15])."""
    # Через ``clock.moscow_today``, а не подменой самого ``datetime`` в модуле: подмена
    # класса заодно ломала все ``datetime.now(UTC)`` рядом — временные метки, к гейту
    # отношения не имеющие. Шов адресует ровно «сегодня по Москве» и ничего больше.
    monkeypatch.setattr(clock, "moscow_today", lambda: date(2026, 5, 15))

    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        await session.commit()

        # Сегодняшней датой (15 мая = день выплаты) аванс отклоняется.
        with pytest.raises(PayrollConflictError):
            await issue_advance(
                session,
                employee_id=emp.id,
                amount=Decimal("1000"),
                allow_loan=False,
                issued_on=date(2026, 5, 15),
                requested_kind="advance",
                payout_method="transfer",
            )

        # Задним числом (14 мая < сегодня) — разрешено: запись уже ушедших денег.
        backdated = await issue_advance(
            session,
            employee_id=emp.id,
            amount=Decimal("1000"),
            allow_loan=False,
            issued_on=date(2026, 5, 14),
            requested_kind="advance",
            payout_method="transfer",
        )
        assert backdated.kind == "advance"

        # Явный заём в сам день выплаты — проходит.
        loan = await issue_advance(
            session,
            employee_id=emp.id,
            amount=Decimal("1000"),
            allow_loan=True,
            issued_on=date(2026, 5, 15),
            requested_kind="loan",
            payout_method="transfer",
        )
        assert loan.kind == "loan"


async def test_issue_advance_within_earned_and_decrements_available(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        await session.commit()

        adv = await issue_advance(
            session,
            employee_id=emp.id,
            amount=Decimal("10000"),
            allow_loan=False,
            issued_on=AS_OF,
            payout_method="transfer",
        )
        assert adv.kind == "advance"
        assert adv.installments_count == 1
        assert adv.per_installment_amount == Decimal("10000.00")
        assert adv.role == "Управляющий"

        avail = await available_to_advance(session, emp, AS_OF)
        assert avail.already_advanced == Decimal("10000.00")
        assert avail.available == Decimal("5000.00")


async def test_issue_over_earned_requires_loan_right(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        await session.commit()

        # Доступно 15000; 20000 сверху → заём; без права отклоняется.
        with pytest.raises(PayrollConflictError):
            await issue_advance(
                session,
                employee_id=emp.id,
                amount=Decimal("20000"),
                allow_loan=False,
                issued_on=AS_OF,
            )


async def test_issue_loan_splits_into_equal_installments(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        await session.commit()

        loan = await issue_advance(
            session,
            employee_id=emp.id,
            amount=Decimal("20000"),
            allow_loan=True,
            installments_count=4,
            issued_on=AS_OF,
            payout_method="cash",
        )
        assert loan.kind == "loan"
        assert loan.installments_count == 4
        assert loan.per_installment_amount == Decimal("5000.00")
        assert loan.recovered_amount == Decimal("0")
        assert loan.status == "issued"


async def test_explicit_loan_within_earned(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Явный заём оформляется даже в пределах заработанного и уменьшает доступное."""
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        await session.commit()

        # Доступно 15000; просим 10000 явным займом → заём, не аванс.
        loan = await issue_advance(
            session,
            employee_id=emp.id,
            amount=Decimal("10000"),
            allow_loan=True,
            requested_kind="loan",
            installment_amount=Decimal("5000"),
            issued_on=AS_OF,
        )
        assert loan.kind == "loan"
        assert loan.installments_count == 2  # 10000 / 5000
        assert loan.per_installment_amount == Decimal("5000.00")

        # Заём в пределах заработанного всё равно уменьшает доступное к авансу.
        avail = await available_to_advance(session, emp, AS_OF)
        assert avail.already_advanced == Decimal("10000.00")
        assert avail.available == Decimal("5000.00")


async def test_loan_installment_amount_derives_count(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Сумма доли задаёт число периодов (округление вверх)."""
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        await session.commit()

        loan = await issue_advance(
            session,
            employee_id=emp.id,
            amount=Decimal("30000"),
            allow_loan=True,
            installment_amount=Decimal("5000"),
            issued_on=AS_OF,
        )
        assert loan.kind == "loan"
        assert loan.installments_count == 6  # 30000 / 5000
        assert loan.per_installment_amount == Decimal("5000.00")


async def test_explicit_advance_over_earned_rejected(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Явный аванс сверх заработанного отклоняется (нужно оформить заём)."""
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        await session.commit()

        with pytest.raises(PayrollConflictError):
            await issue_advance(
                session,
                employee_id=emp.id,
                amount=Decimal("20000"),
                allow_loan=True,
                requested_kind="advance",
                issued_on=AS_OF,
            )


async def test_cancel_advance_before_recovery(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        await session.commit()

        adv = await issue_advance(
            session,
            employee_id=emp.id,
            amount=Decimal("8000"),
            allow_loan=False,
            issued_on=AS_OF,
        )
        cancelled = await cancel_advance(session, adv.id)
        assert cancelled.status == "cancelled"
        # Отменённый аванс не уменьшает доступное.
        avail = await available_to_advance(session, emp, AS_OF)
        assert avail.already_advanced == Decimal("0.00")
        assert avail.available == Decimal("15000.00")


# --------------------------------------------------------------------------- #
# Сейм возврата в ведомости (admin run) + финализация/дефинализация
# --------------------------------------------------------------------------- #
async def _make_first_half_period(session: AsyncSession) -> PayrollPeriod:
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="half_month",
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 15),
        payroll_date=date(2026, 5, 15),
        status="open",
    )
    session.add(period)
    await session.flush()
    return period


async def _one_line(session: AsyncSession, run_id: uuid.UUID) -> PayrollLine:
    return (
        await session.scalars(select(PayrollLine).where(PayrollLine.run_id == run_id))
    ).one()


async def test_advance_recovered_in_admin_run_and_finalize(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        period = await _make_first_half_period(session)
        await session.commit()

        adv = await issue_advance(
            session,
            employee_id=emp.id,
            amount=Decimal("10000"),
            allow_loan=False,
            issued_on=AS_OF,
            payout_method="transfer",
        )
        run = await run_admin_payroll(session, period.id)
        assert run.status == "completed"

        line = await _one_line(session, run.id)
        assert line.base_pay == Decimal("45000.00")
        assert line.advance_recovered == Decimal("10000.00")
        assert line.total_payable == Decimal("35000.00")  # 45000 − 10000 удержано
        # Итог ведомости отражает удержание (а не начисления).
        assert run.summary["total_payable"] == 35000.0

        # До финализации возврат — превью: баланс аванса не двинут.
        await session.refresh(adv)
        assert adv.recovered_amount == Decimal("0")
        assert adv.status == "issued"

        await finalize_payroll_run(session, run.id)
        await session.refresh(adv)
        assert adv.recovered_amount == Decimal("10000.00")
        assert adv.status == "recovered"


async def test_recovery_override_full_loan_payoff(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Окно «Удержания»: override = весь остаток → досрочное полное гашение займа."""
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        period = await _make_first_half_period(session)
        await session.commit()

        loan = await issue_advance(
            session,
            employee_id=emp.id,
            amount=Decimal("30000"),
            allow_loan=True,
            requested_kind="loan",
            installments_count=3,
            issued_on=AS_OF,
            payout_method="transfer",
        )
        run = await run_admin_payroll(session, period.id)
        line = await _one_line(session, run.id)
        # По умолчанию удерживается доля 10000 из 30000.
        assert line.advance_recovered == Decimal("10000.00")
        assert line.total_payable == Decimal("35000.00")

        # Сотрудник гасит заём целиком сейчас — override на весь остаток.
        await set_advance_recovery_overrides(
            session,
            run_id=run.id,
            items=[(loan.id, Decimal("30000"))],
        )
        run = await run_admin_payroll(session, period.id)
        line = await _one_line(session, run.id)
        assert line.advance_recovered == Decimal("30000.00")
        assert line.total_payable == Decimal("15000.00")  # 45000 − 30000

        await finalize_payroll_run(session, run.id)
        await session.refresh(loan)
        assert loan.recovered_amount == Decimal("30000.00")
        assert loan.status == "recovered"


async def test_recovery_override_rejects_over_outstanding(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Нельзя удержать больше остатка долга."""
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        period = await _make_first_half_period(session)
        await session.commit()

        loan = await issue_advance(
            session,
            employee_id=emp.id,
            amount=Decimal("30000"),
            allow_loan=True,
            requested_kind="loan",
            installments_count=3,
            issued_on=AS_OF,
            payout_method="transfer",
        )
        run = await run_admin_payroll(session, period.id)
        with pytest.raises(PayrollConflictError):
            await set_advance_recovery_overrides(
                session,
                run_id=run.id,
                items=[(loan.id, Decimal("40000"))],
            )


async def test_unfinalize_reverses_advance_recovery(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        period = await _make_first_half_period(session)
        await session.commit()

        adv = await issue_advance(
            session, employee_id=emp.id, amount=Decimal("10000"), allow_loan=False, issued_on=AS_OF
        )
        run = await run_admin_payroll(session, period.id)
        await finalize_payroll_run(session, run.id)
        await session.refresh(adv)
        assert adv.recovered_amount == Decimal("10000.00")

        await unfinalize_payroll_run(session, run.id, reason="правка", actor_user_id=None)
        await session.refresh(adv)
        assert adv.recovered_amount == Decimal("0")
        assert adv.status == "issued"


async def test_loan_installment_recovered_per_run(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        period = await _make_first_half_period(session)
        await session.commit()

        # Доступно 15000; заём 20000 на 4 периода → доля 5000.
        loan = await issue_advance(
            session,
            employee_id=emp.id,
            amount=Decimal("20000"),
            allow_loan=True,
            installments_count=4,
            issued_on=AS_OF,
        )
        run = await run_admin_payroll(session, period.id)
        line = await _one_line(session, run.id)
        # За один прогон гасится одна доля.
        assert line.advance_recovered == Decimal("5000.00")
        assert line.total_payable == Decimal("40000.00")

        await finalize_payroll_run(session, run.id)
        await session.refresh(loan)
        assert loan.recovered_amount == Decimal("5000.00")
        assert loan.status == "issued"  # остаются 3 доли


async def test_recovery_start_date_defers_recovery(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Заём с датой начала удержания позже периода в этой ведомости не гасится."""
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        period = await _make_first_half_period(session)  # 1–15 мая
        await session.commit()

        loan = await issue_advance(
            session,
            employee_id=emp.id,
            amount=Decimal("10000"),
            allow_loan=True,
            requested_kind="loan",
            installment_amount=Decimal("5000"),
            issued_on=AS_OF,
            recovery_start_date=date(2026, 5, 16),  # после конца периода
        )
        run = await run_admin_payroll(session, period.id)
        line = await _one_line(session, run.id)
        # Удержания нет — заём ещё «спит».
        assert line.advance_recovered == Decimal("0.00")
        assert line.total_payable == Decimal("45000.00")

        await finalize_payroll_run(session, run.id)
        await session.refresh(loan)
        assert loan.recovered_amount == Decimal("0")
        assert loan.status == "issued"


async def test_defer_advance_recovery_skips_this_run_then_resumes(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Отсрочка освобождает заём от удержания в ведомости; возврат — снова удерживает."""
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        period = await _make_first_half_period(session)
        await session.commit()

        loan = await issue_advance(
            session,
            employee_id=emp.id,
            amount=Decimal("10000"),
            allow_loan=True,
            requested_kind="loan",
            installment_amount=Decimal("5000"),
            issued_on=AS_OF,
        )
        run = await run_admin_payroll(session, period.id)
        line = await _one_line(session, run.id)
        assert line.advance_recovered == Decimal("5000.00")  # по умолчанию гасится
        assert run.summary["total_payable"] == 40000.0  # итог отражает удержание

        # Отсрочка + пересчёт → удержания нет, итог растёт.
        await set_advance_recovery_deferral(
            session, run_id=run.id, advance_id=loan.id, defer=True, actor_user_id=None
        )
        run_after_defer = await run_admin_payroll(session, period.id)
        line_after_defer = await _one_line(session, run.id)
        assert line_after_defer.advance_recovered == Decimal("0.00")
        assert line_after_defer.total_payable == Decimal("45000.00")
        assert run_after_defer.summary["total_payable"] == 45000.0

        # Возврат удержания + пересчёт → снова гасится, итог падает.
        await set_advance_recovery_deferral(
            session, run_id=run.id, advance_id=loan.id, defer=False, actor_user_id=None
        )
        run_after_resume = await run_admin_payroll(session, period.id)
        line_after_resume = await _one_line(session, run.id)
        assert line_after_resume.advance_recovered == Decimal("5000.00")
        assert run_after_resume.summary["total_payable"] == 40000.0


async def test_recovery_capped_by_line_net_anti_negative(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("6000"))
        period = await _make_first_half_period(session)
        await session.commit()

        # База полупериода 3000; доступно на 5-е = 1000. Заём 5000 одной долей.
        loan = await issue_advance(
            session,
            employee_id=emp.id,
            amount=Decimal("5000"),
            allow_loan=True,
            installments_count=1,
            issued_on=AS_OF,
        )
        run = await run_admin_payroll(session, period.id)
        line = await _one_line(session, run.id)
        # Гасим только net (3000), выплата не уходит в минус.
        assert line.total_payable == Decimal("0.00")
        assert line.advance_recovered == Decimal("3000.00")

        await finalize_payroll_run(session, run.id)
        await session.refresh(loan)
        assert loan.recovered_amount == Decimal("3000.00")
        assert loan.status == "issued"  # остаток 2000 переносится


async def test_written_off_advance_excluded_from_recovery(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        period = await _make_first_half_period(session)
        await session.commit()

        adv = await issue_advance(
            session, employee_id=emp.id, amount=Decimal("10000"), allow_loan=False, issued_on=AS_OF
        )
        written = await write_off_advance(session, adv.id, reason="увольнение")
        assert written.status == "written_off"

        # Списанный аванс сейм возврата не подхватывает.
        run = await run_admin_payroll(session, period.id)
        line = await _one_line(session, run.id)
        assert line.advance_recovered == Decimal("0.00")
        assert line.total_payable == Decimal("45000.00")


# --------------------------------------------------------------------------- #
# Потолок займа
# --------------------------------------------------------------------------- #
async def test_loan_ceiling_blocks_without_override(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        await session.commit()
        # Заём 150000 > дефолтного потолка 100000 → блок без подтверждения.
        with pytest.raises(PayrollConflictError):
            await issue_advance(
                session,
                employee_id=emp.id,
                amount=Decimal("150000"),
                allow_loan=True,
                installments_count=1,
                issued_on=AS_OF,
            )


async def test_loan_ceiling_override_allows(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        await session.commit()
        loan = await issue_advance(
            session,
            employee_id=emp.id,
            amount=Decimal("150000"),
            allow_loan=True,
            override_ceiling=True,
            installments_count=1,
            issued_on=AS_OF,
        )
        assert loan.kind == "loan"
        assert loan.amount == Decimal("150000.00")


async def test_set_loan_max_raises_ceiling(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        await set_loan_max(session, Decimal("200000"))
        await session.commit()
        loan = await issue_advance(
            session,
            employee_id=emp.id,
            amount=Decimal("150000"),
            allow_loan=True,
            installments_count=1,
            issued_on=AS_OF,
        )
        assert loan.kind == "loan"


async def test_cannot_issue_into_finalized_period(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        period = await _make_first_half_period(session)
        await session.commit()

        run = await run_admin_payroll(session, period.id)
        await finalize_payroll_run(session, run.id)

        # AS_OF (5 мая) внутри финализированного периода 1–15 мая → выдача заблокирована.
        with pytest.raises(PayrollConflictError):
            await issue_advance(
                session,
                employee_id=emp.id,
                amount=Decimal("5000"),
                allow_loan=False,
                issued_on=AS_OF,
            )


async def _advance_cashflow(
    factory: async_sessionmaker[AsyncSession], advance_id: uuid.UUID
) -> CashflowTransaction | None:
    async with factory() as session:
        return await session.scalar(
            select(CashflowTransaction).where(
                CashflowTransaction.source_kind == "salary_advance",
                CashflowTransaction.source_id == advance_id,
            )
        )


async def test_advance_payout_books_dds_cashflow_with_advance_article(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Выдача аванса наличными (ТК Черникова) → расход в ДДС со статьёй «Авансы сотрудникам»."""
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        wallet = await session.scalar(select(Wallet).where(Wallet.code == "tk_chernikova"))
        await session.commit()

        adv = await issue_advance(
            session,
            employee_id=emp.id,
            amount=Decimal("10000"),
            allow_loan=False,
            issued_on=AS_OF,
            payout_method="cash",
            wallet_id=wallet.id,
        )

    txn = await _advance_cashflow(async_session_factory, adv.id)
    assert txn is not None
    assert txn.direction == "out"
    assert txn.amount == Decimal("10000.00")
    async with async_session_factory() as session:
        article = await session.get(DdsArticle, txn.article_id)
    assert article is not None and article.code == "employee_advance"


async def test_loan_payout_uses_loan_article(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Выдача займа наличными → статья «Выдача займов сотрудникам» (отдельная от аванса)."""
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        wallet = await session.scalar(select(Wallet).where(Wallet.code == "cash_safe"))
        await session.commit()

        loan = await issue_advance(
            session,
            employee_id=emp.id,
            amount=Decimal("50000"),
            allow_loan=True,
            requested_kind="loan",
            issued_on=AS_OF,
            payout_method="cash",
            wallet_id=wallet.id,
        )

    txn = await _advance_cashflow(async_session_factory, loan.id)
    assert txn is not None
    async with async_session_factory() as session:
        article = await session.get(DdsArticle, txn.article_id)
    assert article is not None and article.code == "vydacha_zaymov_sotrudnikam"


async def test_cash_advance_tk_chernikova_triggers_iiko(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Наличная выдача с ТК Черникова (= iiko «Главная касса») → изъятие в iiko (source='cash')."""
    calls: list[dict] = []

    async def _record(_session: AsyncSession, **kw: object) -> None:
        calls.append(kw)

    monkeypatch.setattr(
        "app.services.payroll_advance_service.post_advance_payout_to_iiko",
        _record,
    )
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        wallet = await session.scalar(select(Wallet).where(Wallet.code == "tk_chernikova"))
        await session.commit()
        adv = await issue_advance(
            session,
            employee_id=emp.id,
            amount=Decimal("10000"),
            allow_loan=False,
            issued_on=AS_OF,
            payout_method="cash",
            wallet_id=wallet.id,
        )
    assert len(calls) == 1
    assert calls[0]["source"] == "cash"
    assert calls[0]["is_loan"] is False
    assert calls[0]["source_id"] == str(adv.id)
    assert calls[0]["amount"] == Decimal("10000.00")


async def test_cash_advance_safe_does_not_trigger_iiko(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Наличная выдача с Сейфа → проводка ДДС есть, но iiko НЕ дёргается (Сейф ≠ Главная касса)."""
    calls: list[dict] = []
    monkeypatch.setattr(
        "app.services.payroll_advance_service.post_advance_payout_to_iiko",
        lambda **kw: calls.append(kw),
    )
    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        wallet = await session.scalar(select(Wallet).where(Wallet.code == "cash_safe"))
        await session.commit()
        await issue_advance(
            session,
            employee_id=emp.id,
            amount=Decimal("10000"),
            allow_loan=False,
            issued_on=AS_OF,
            payout_method="cash",
            wallet_id=wallet.id,
        )
    assert calls == []


async def test_bank_wallet_payout_creates_draft_no_cashflow(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Выдача через банк-кошелёк (Фаза 2): расхода ДДС нет, но создан черновик платежа,
    а аванс встаёт «ожидает выплаты» (долг сформируется только при «Выплачено»)."""
    from app.models import SalaryAdvanceBankDraft

    async with async_session_factory() as session:
        emp = await _make_okladnik(session)
        await _set_oklad(session, position="Управляющий", amount=Decimal("90000"))
        wallet = await session.scalar(select(Wallet).where(Wallet.code == "tbank_main"))
        await session.commit()

        adv = await issue_advance(
            session,
            employee_id=emp.id,
            amount=Decimal("10000"),
            allow_loan=False,
            issued_on=AS_OF,
            payout_method="transfer",
            wallet_id=wallet.id,
        )

    assert adv.status == "awaiting_payout"
    assert await _advance_cashflow(async_session_factory, adv.id) is None
    async with async_session_factory() as session:
        draft = await session.scalar(
            select(SalaryAdvanceBankDraft).where(SalaryAdvanceBankDraft.advance_id == adv.id)
        )
    assert draft is not None
    assert draft.status == "created"

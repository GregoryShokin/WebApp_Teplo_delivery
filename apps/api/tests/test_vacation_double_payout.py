"""Выплаченный отпуск нельзя выплатить вторым траншем.

Дефект: отпускные выдаются одним траншем по совпадению ``payout_date`` с ``payroll_date``
ведомости, но сам отпуск не помнил, что деньги уже ушли. Достаточно было перенести
``payout_date`` на другой незакрытый вторник — и те же 10 000 ₽ начислялись повторно.
Решение владельца 02.08.2026 — жёсткий запрет: пока оплатившая ведомость финализирована,
даты и отмена заморожены; разморозка — только через дефинализацию.

Тесты работают на РЕАЛЬНОЙ сессии: `mark_vacations_paid_for_payroll_period` до сих пор была
замокана во всех тестах раннера и не исполнялась ни разу, поэтому фейковая сессия здесь
воспроизвела бы ту же слепоту.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentActor
from app.models import (
    Employee,
    EmployeePositionAssignment,
    PayrollLine,
    PayrollPeriod,
    PayrollRun,
    VacationPeriod,
)
from app.services import vacation_service
from app.services.payroll_runner import run_has_stale_vacation_payouts

WEEK_START = date(2026, 6, 2)
WEEK_END = date(2026, 6, 8)
PAYROLL_DATE = date(2026, 6, 9)  # вторник
NEXT_PAYROLL_DATE = date(2026, 6, 16)  # следующий вторник — куда пытаются перенести
VACATION_START = date(2026, 6, 10)
VACATION_END = date(2026, 6, 12)
VACATION_DAYS = 3


def _actor() -> CurrentActor:
    return CurrentActor(roles=frozenset({"finance_manager"}), user_id=None)


async def _make_cook(session: AsyncSession, name: str = "Повар Отпускник") -> Employee:
    employee = Employee(
        id=uuid.uuid4(),
        full_name=name,
        iiko_id=f"iiko-{uuid.uuid4()}",
        position="Повар",
        status="active",
        is_senior=False,
        is_deputy_senior=False,
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


async def _make_week(
    session: AsyncSession, *, payroll_date: date = PAYROLL_DATE, status: str = "finalized"
) -> PayrollPeriod:
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="week",
        start_date=payroll_date - timedelta(days=7),
        end_date=payroll_date - timedelta(days=1),
        payroll_date=payroll_date,
        status=status,
    )
    session.add(period)
    await session.flush()
    return period


async def _make_vacation(
    session: AsyncSession,
    employee: Employee,
    *,
    paid_period: PayrollPeriod | None,
    status: str = "paid",
) -> VacationPeriod:
    period = VacationPeriod(
        id=uuid.uuid4(),
        employee_id=employee.id,
        date_start=VACATION_START,
        date_end=VACATION_END,
        days_count=VACATION_DAYS,
        payout_date=PAYROLL_DATE,
        status=status,
        paid_period_id=paid_period.id if paid_period else None,
        paid_at=datetime.now(UTC) if paid_period else None,
    )
    session.add(period)
    await session.flush()
    return period


async def test_settled_vacation_cannot_be_moved(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Главный кейс: перенос даты выплаты у оплаченного отпуска — 422, а не второй транш."""
    async with async_session_factory() as session:
        cook = await _make_cook(session)
        payslip = await _make_week(session)
        # Ведомость, в которую пытаются перенести, — открыта, сама по себе дата валидна.
        await _make_week(session, payroll_date=NEXT_PAYROLL_DATE, status="open")
        vacation = await _make_vacation(session, cook, paid_period=payslip)
        await session.commit()

        with pytest.raises(HTTPException) as exc:
            await vacation_service.update_vacation_period(
                session,
                vacation.id,
                payout_date=NEXT_PAYROLL_DATE,
                actor=_actor(),
            )
        assert exc.value.status_code == 422
        assert "уже выплачены ведомостью" in exc.value.detail
        assert "09.06.2026" in exc.value.detail

        await session.refresh(vacation)
        assert vacation.payout_date == PAYROLL_DATE
        assert vacation.status == "paid"


async def test_settled_vacation_cannot_be_cancelled(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Отмена тоже заморожена: деньги ушли, а отмена вернула бы дни в годовой лимит."""
    async with async_session_factory() as session:
        cook = await _make_cook(session)
        payslip = await _make_week(session)
        vacation = await _make_vacation(session, cook, paid_period=payslip)
        await session.commit()

        with pytest.raises(HTTPException) as exc:
            await vacation_service.cancel_vacation_period(session, vacation.id, actor=_actor())
        assert exc.value.status_code == 422

        await session.refresh(vacation)
        assert vacation.status == "paid"


async def test_comment_edit_allowed_and_keeps_paid(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Комментарий денег не двигает — править можно, и статус «оплачен» не теряется.

    Раньше ЛЮБОЙ успешный PATCH сбрасывал paid → planned, и запись переставала помнить,
    что деньги ушли.
    """
    async with async_session_factory() as session:
        cook = await _make_cook(session)
        payslip = await _make_week(session)
        vacation = await _make_vacation(session, cook, paid_period=payslip)
        await session.commit()

        updated = await vacation_service.update_vacation_period(
            session, vacation.id, comment="уточнение", actor=_actor()
        )
        assert updated.comment == "уточнение"
        assert updated.status == "paid"
        assert updated.paid_period_id == payslip.id


async def test_open_payslip_does_not_freeze_vacation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Заморозка — по ФИНАЛИЗАЦИИ, а не по статусу «оплачен».

    `paid` ставится на РАСЧЁТЕ ведомости, то есть означает «попало в расчёт». Пока
    ведомость открыта, отпуск правится свободно — пересчёт подхватит изменения.
    """
    async with async_session_factory() as session:
        cook = await _make_cook(session)
        payslip = await _make_week(session, status="open")
        await _make_week(session, payroll_date=NEXT_PAYROLL_DATE, status="open")
        vacation = await _make_vacation(session, cook, paid_period=payslip)
        await session.commit()

        updated = await vacation_service.update_vacation_period(
            session, vacation.id, payout_date=NEXT_PAYROLL_DATE, actor=_actor()
        )
        assert updated.payout_date == NEXT_PAYROLL_DATE
        # Даты сдвинулись — отметка «оплачен» снята, ведомость пересчитает заново.
        assert updated.status == "planned"


async def test_unfinalize_releases_the_freeze(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Дефинализация снимает отметку — иначе ошибку нельзя было бы исправить никогда."""
    async with async_session_factory() as session:
        cook = await _make_cook(session)
        payslip = await _make_week(session)
        await _make_week(session, payroll_date=NEXT_PAYROLL_DATE, status="open")
        vacation = await _make_vacation(session, cook, paid_period=payslip)
        await session.commit()

        released = await vacation_service.revert_vacation_paid_status_for_period(
            session, payslip.id
        )
        assert released == 1
        payslip.status = "open"  # дефинализация открывает период
        await session.commit()

        await session.refresh(vacation)
        # Статус откатился, но ЯКОРЬ остался: заморозка считается по статусу ведомости,
        # а стёртый якорь не вернулся бы при повторной финализации без пересчёта.
        assert vacation.paid_period_id == payslip.id
        assert vacation.status == "planned"

        # После разморозки перенос снова разрешён.
        updated = await vacation_service.update_vacation_period(
            session, vacation.id, payout_date=NEXT_PAYROLL_DATE, actor=_actor()
        )
        assert updated.payout_date == NEXT_PAYROLL_DATE


async def test_mark_paid_records_the_paying_payslip(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Реальный `mark_vacations_paid_for_payroll_period` записывает, КТО заплатил.

    До сих пор эта функция была замокана во всех тестах раннера и не исполнялась ни разу.
    """
    async with async_session_factory() as session:
        cook = await _make_cook(session)
        payslip = await _make_week(session, status="open")
        vacation = await _make_vacation(session, cook, paid_period=None, status="planned")
        await session.commit()

        count = await vacation_service.mark_vacations_paid_for_payroll_period(
            session,
            period_start=payslip.start_date,
            period_end=payslip.end_date,
            payroll_date=payslip.payroll_date,
            payroll_period_id=payslip.id,
        )
        assert count == 1
        await session.commit()

        await session.refresh(vacation)
        assert vacation.status == "paid"
        assert vacation.paid_period_id == payslip.id
        assert vacation.paid_at is not None


async def _run_with_baked_lump(
    session: AsyncSession, employee: Employee, payslip: PayrollPeriod, amount: str
) -> PayrollRun:
    """Расчёт, в строках которого уже запечён отпускной транш."""
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=payslip.id,
        status="completed",
        is_imported_legacy=False,
        started_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()
    session.add(
        PayrollLine(
            id=uuid.uuid4(),
            run_id=run.id,
            employee_id=employee.id,
            role="pizza",
            vacation_pay=Decimal(amount),
            total_payable=Decimal(amount),
            components={
                "days": [
                    {
                        "kind": "vacation_payout",
                        "date": payslip.payroll_date.isoformat(),
                        "vacation_pay": float(amount),
                    }
                ]
            },
        )
    )
    await session.flush()
    return run


async def test_finalize_blocked_when_vacation_moved_after_calculation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Последний путь к двойной выплате: финализация УСТАРЕВШЕГО расчёта.

    finalize_payroll_run не пересчитывает ведомость. Поэтому после дефинализации можно было
    перенести отпуск на другую дату и финализировать старый расчёт с его траншем — деньги
    ушли бы дважды. Гард сверяет запечённые транши с текущим состоянием отпусков.
    """
    async with async_session_factory() as session:
        cook = await _make_cook(session)
        payslip = await _make_week(session, status="open")
        run = await _run_with_baked_lump(session, cook, payslip, "3000")
        vacation = await _make_vacation(session, cook, paid_period=None, status="planned")
        await session.commit()

        # Пока отпуск на месте — расчёт актуален.
        assert await run_has_stale_vacation_payouts(session, run, payslip) is False

        # Отпуск уехал на другую дату выплаты → расчёт устарел.
        vacation.payout_date = NEXT_PAYROLL_DATE
        await session.commit()
        assert await run_has_stale_vacation_payouts(session, run, payslip) is True

        # Отмена отпуска — тоже устаревание.
        vacation.payout_date = PAYROLL_DATE
        vacation.status = "cancelled"
        await session.commit()
        assert await run_has_stale_vacation_payouts(session, run, payslip) is True


async def test_refinalize_after_unfinalize_restores_the_freeze(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Цикл «откатил → вернул обратно без пересчёта» НЕ снимает защиту.

    Найдено состязательным разбором: если дефинализация стирает якорь, повторная
    финализация его не восстановит (якорь ставит только расчёт) — и отпуск остаётся
    размороженным при деньгах в закрытой ведомости.
    """
    async with async_session_factory() as session:
        cook = await _make_cook(session)
        payslip = await _make_week(session)
        vacation = await _make_vacation(session, cook, paid_period=payslip)
        await session.commit()

        # Дефинализация: период открыт, отпуск разморожен.
        await vacation_service.revert_vacation_paid_status_for_period(session, payslip.id)
        payslip.status = "open"
        await session.commit()
        assert await vacation_service.settling_finalized_period(session, vacation) is None

        # Финализировали обратно, ничего не пересчитывая, — заморозка вернулась сама.
        payslip.status = "finalized"
        await session.commit()
        settled = await vacation_service.settling_finalized_period(session, vacation)
        assert settled is not None and settled.id == payslip.id


async def test_allowed_edit_clears_the_anchor(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Отпуск уехал на другую дату — якорь уехал вместе с ним.

    Иначе после финализации СТАРОЙ ведомости отпуск заморозился бы чужой выплатой,
    а settled_payout_date показал бы неверную дату.
    """
    async with async_session_factory() as session:
        cook = await _make_cook(session)
        payslip = await _make_week(session, status="open")
        await _make_week(session, payroll_date=NEXT_PAYROLL_DATE, status="open")
        vacation = await _make_vacation(session, cook, paid_period=payslip)
        await session.commit()

        updated = await vacation_service.update_vacation_period(
            session, vacation.id, payout_date=NEXT_PAYROLL_DATE, actor=_actor()
        )
        assert updated.paid_period_id is None
        assert updated.paid_at is None

        payslip.status = "finalized"
        await session.commit()
        assert await vacation_service.settling_finalized_period(session, updated) is None


async def test_guard_ignores_half_month_runs(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Админская (полумесячная) ведомость не должна блокироваться отпускными.

    Её payroll_date может совпасть со вторником — и без фильтра по типу периода она
    становилась НЕФИНАЛИЗИРУЕМОЙ навсегда: строк отпуска в ней нет, а лумп по дате есть.
    """
    async with async_session_factory() as session:
        cook = await _make_cook(session)
        admin_period = PayrollPeriod(
            id=uuid.uuid4(),
            period_type="half_month",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 15),
            payroll_date=PAYROLL_DATE,  # вторник — совпадает с payout_date отпуска
            status="open",
        )
        session.add(admin_period)
        await session.flush()
        run = PayrollRun(
            id=uuid.uuid4(),
            period_id=admin_period.id,
            status="completed",
            is_imported_legacy=False,
            started_at=datetime.now(UTC),
        )
        session.add(run)
        await _make_vacation(session, cook, paid_period=None, status="planned")
        await session.commit()

        assert await run_has_stale_vacation_payouts(session, run, admin_period) is False


async def test_payout_skips_vacation_settled_by_another_payslip(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ремень на стороне денег: транш, выплаченный закрытой ведомостью, не выдаётся снова."""
    async with async_session_factory() as session:
        cook = await _make_cook(session)
        paid_by = await _make_week(session)  # финализирована
        target = await _make_week(session, payroll_date=NEXT_PAYROLL_DATE, status="open")
        vacation = await _make_vacation(session, cook, paid_period=paid_by)
        # Запись каким-то путём оказалась на дате другой ведомости.
        vacation.payout_date = NEXT_PAYROLL_DATE
        await session.commit()

        payouts = await vacation_service.vacation_payouts_for_payroll_date(
            session, payroll_date=target.payroll_date
        )
        assert payouts == []


async def test_date_shift_keeps_the_anchor_when_payout_date_unchanged(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Сдвиг дат отпуска при ПРЕЖНЕЙ дате выплаты не снимает якорь.

    Найдено вторым кругом разбора: платящую ведомость определяет только payout_date, и если
    стирать якорь на любой сдвиг дат, то ведомость всё ещё платит транш, а отпуск уже
    разморожен — восстановить якорь может лишь пересчёт. Дальше перенос payout_date давал
    второй транш штатным действием оператора.
    """
    async with async_session_factory() as session:
        cook = await _make_cook(session)
        payslip = await _make_week(session, status="open")
        vacation = await _make_vacation(session, cook, paid_period=payslip)
        await session.commit()

        updated = await vacation_service.update_vacation_period(
            session,
            vacation.id,
            date_start=VACATION_START + timedelta(days=1),
            date_end=VACATION_END + timedelta(days=1),
            actor=_actor(),
        )
        assert updated.paid_period_id == payslip.id  # якорь на месте

        payslip.status = "finalized"
        await session.commit()
        settled = await vacation_service.settling_finalized_period(session, updated)
        assert settled is not None and settled.id == payslip.id


async def test_per_day_vacation_is_anchored_by_the_first_paying_week(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Подённый отпуск (без payout_date) морозится с ПЕРВЫХ ушедших денег.

    Он может пересекать несколько ведомостей, и каждая платит свои дни. Раньше якорь
    ставила только та неделя, где отпуск заканчивается, — до неё отпуск с уже оплаченной
    половиной свободно переносился целиком.
    """
    async with async_session_factory() as session:
        cook = await _make_cook(session)
        first_week = await _make_week(session, payroll_date=PAYROLL_DATE, status="open")
        vacation = VacationPeriod(
            id=uuid.uuid4(),
            employee_id=cook.id,
            # Начинается внутри первой недели, а заканчивается уже за её пределами.
            date_start=first_week.start_date + timedelta(days=2),
            date_end=first_week.end_date + timedelta(days=3),
            days_count=8,
            payout_date=None,
            status="planned",
        )
        session.add(vacation)
        await session.commit()

        await vacation_service.mark_vacations_paid_for_payroll_period(
            session,
            period_start=first_week.start_date,
            period_end=first_week.end_date,
            payroll_date=first_week.payroll_date,
            payroll_period_id=first_week.id,
        )
        await session.commit()
        await session.refresh(vacation)

        # Отпуск ещё не закончился — статус остался planned, но якорь уже стоит.
        assert vacation.status == "planned"
        assert vacation.paid_period_id == first_week.id

        first_week.status = "finalized"
        await session.commit()
        settled = await vacation_service.settling_finalized_period(session, vacation)
        assert settled is not None and settled.id == first_week.id


async def test_cancel_clears_the_anchor(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Отменённый отпуск не оплачивается — метка «выплачено» на нём ложна."""
    async with async_session_factory() as session:
        cook = await _make_cook(session)
        payslip = await _make_week(session, status="open")
        vacation = await _make_vacation(session, cook, paid_period=payslip)
        await session.commit()

        cancelled = await vacation_service.cancel_vacation_period(
            session, vacation.id, actor=_actor()
        )
        assert cancelled.status == "cancelled"
        assert cancelled.paid_period_id is None
        assert cancelled.paid_at is None

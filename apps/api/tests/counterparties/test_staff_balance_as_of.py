"""Расчёты с сотрудниками НА ДАТУ — вторая половина баланса.

Витрина «Расчёты с сотрудниками» (``/accounting/suppliers/staff-payable``) отвечает на вопрос
«сколько должны СЕЙЧАС» и делает это ТЕКУЩИМИ СТАТУСАМИ. Статус истории не хранит: заём,
списанный в сентябре, на 31 августа был живой дебиторкой, а по ``status='written_off'`` его
там уже нет; фонд уволенного 31 августа был долгом, а по ``AccumulationFundAccount.status``
его нет ни на одну дату. Баланс собирается на конец месяца, и такими цифрами его не собрать.

Здесь закреплены свойства среза ``build_staff_balance_as_of``:

* у каждого слагаемого своя ХОЗЯЙСТВЕННАЯ дата, и ни одна из них не «сейчас»;
* ведомость, финализированная позже даты среза, приносит на эту дату ЗАРАБОТАННОЕ, а после
  финализации — хвост, и ни на одной дате эти две величины не складываются;
* «выплачено» считается по иммутабельному леджеру ``PayrollPayoutBooking``, а не по отметке
  выплаты: строка отметки при снятии удаляется физически и прошлое стирает вместе с собой;
* до 01.07.2026 срез отказывает, а не выдумывает цифру из отсутствия движений.

Тесты сервисные: собирают данные прямо в сессии и зовут расчёт. Реальный калькулятор
ведомости подменяется только там, где проверяется ДАТИРОВКА заработанного, — сама формула
заработка покрыта отдельно (``tests/test_payroll*``), и тащить сюда ставки, категории и
проценты значило бы проверять чужой контур.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from cp_helpers import admin_headers, make_wallet
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    AccumulationFundAccount,
    AccumulationFundTransaction,
    AttendanceEntry,
    CashflowTransaction,
    DepositTransaction,
    Employee,
    EmployeePositionAssignment,
    PayrollLine,
    PayrollPayment,
    PayrollPayoutBooking,
    PayrollPeriod,
    PayrollRun,
    SalaryAdvance,
)
from app.services import payroll_advance_availability as avail
from app.services.payroll_calculator import PayrollCalculationResult
from app.services.staff_balance_as_of import (
    BeforeAccountingStart,
    StaffBalanceAsOf,
    StaffBalanceRow,
    build_staff_balance_as_of,
)

ZERO = Decimal("0.00")


# --- фабрики -------------------------------------------------------------------


async def _make_employee(
    session: AsyncSession,
    *,
    name: str,
    position: str = "Повар",
    status: str = "active",
    fire_date: date | None = None,
) -> Employee:
    """Сотрудник с должностью НА ИСТОРИЮ: назначение открыто с 01.01.2026.

    Должность заводится строкой ``EmployeePositionAssignment``, а не полем: срез спрашивает
    её ``get_position_on_date`` — то есть на дату среза, а не на сегодня.
    """
    employee = Employee(
        id=uuid.uuid4(),
        full_name=name,
        iiko_id=f"iiko-{uuid.uuid4()}",
        status=status,
        fire_date=fire_date,
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


async def _make_week(
    session: AsyncSession,
    *,
    start: date,
    end: date,
    payroll_date: date,
    finalized_at: datetime | None = None,
) -> PayrollPeriod:
    """Недельный период. ``finalized_at`` — ДЕНЬ ЗАКРЫТИЯ ведомости, а не конец периода.

    Именно расхождение этих двух дат ломало витрину: неделя 25–31.08 закрывается 3 сентября,
    и «множество финализированных периодов» без даты финализации считает её закрытой уже
    31 августа.
    """
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="week",
        start_date=start,
        end_date=end,
        payroll_date=payroll_date,
        status="finalized" if finalized_at is not None else "open",
        finalized_at=finalized_at,
    )
    session.add(period)
    await session.flush()
    return period


async def _make_run(
    session: AsyncSession, period: PayrollPeriod, *, status: str = "finalized"
) -> PayrollRun:
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=period.id,
        status=status,
        is_imported_legacy=False,
        started_at=datetime(
            period.end_date.year, period.end_date.month, period.end_date.day, tzinfo=UTC
        ),
    )
    session.add(run)
    await session.flush()
    return run


async def _make_line(
    session: AsyncSession,
    *,
    run: PayrollRun,
    employee: Employee,
    total_payable: str,
    role: str = "Повар",
) -> PayrollLine:
    line = PayrollLine(
        id=uuid.uuid4(),
        run_id=run.id,
        employee_id=employee.id,
        role=role,
        total_payable=Decimal(total_payable),
    )
    session.add(line)
    await session.flush()
    return line


async def _make_booking(
    session: AsyncSession,
    *,
    run: PayrollRun,
    employee: Employee,
    wallet_id: uuid.UUID,
    amount: str,
    paid_on: date,
    reversed_on: date | None = None,
) -> PayrollPayoutBooking:
    """Иммутабельный след выплаты: расход ДДС + (опционально) его финансовый реверс.

    Дата выплаты живёт в ``CashflowTransaction.operation_date``, дата снятия отметки — в
    операционной дате реверса. Обе переживают удаление строки ``payroll_payment``.
    """
    original = CashflowTransaction(
        id=uuid.uuid4(),
        wallet_id=wallet_id,
        direction="out",
        amount=Decimal(amount),
        operation_date=paid_on,
        source_kind="payroll_payout",
        quality_status="auto",
    )
    session.add(original)
    await session.flush()
    reversal_id = None
    if reversed_on is not None:
        reversal = CashflowTransaction(
            id=uuid.uuid4(),
            wallet_id=wallet_id,
            direction="in",
            amount=Decimal(amount),
            operation_date=reversed_on,
            source_kind="payroll_payout_reversal",
            quality_status="auto",
        )
        session.add(reversal)
        await session.flush()
        reversal_id = reversal.id
    booking = PayrollPayoutBooking(
        id=uuid.uuid4(),
        run_id=run.id,
        employee_id=employee.id,
        payment_id=None,
        cashflow_transaction_id=original.id,
        reversal_transaction_id=reversal_id,
        amount=Decimal(amount),
        reversed_at=(
            datetime(reversed_on.year, reversed_on.month, reversed_on.day, tzinfo=UTC)
            if reversed_on is not None
            else None
        ),
    )
    session.add(booking)
    await session.flush()
    return booking


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
        is_open=False,
    )


def _row(report: StaffBalanceAsOf, employee_id: uuid.UUID) -> StaffBalanceRow | None:
    return next((row for row in report.rows if row.employee_id == employee_id), None)


# --- сценарии ------------------------------------------------------------------


async def test_week_finalized_next_month_shows_earned_then_tail_without_doubling(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    """ГЛАВНЫЙ: неделя 25–31.08 закрыта 3 сентября. Зарплата за неё не исчезает из августа.

    Дефект, который ловим, — ``finalized_bounds`` витрины: она сравнивает границы периода с
    множеством ВСЕХ финализированных периодов, не глядя на дату финализации. На 31 августа
    неделя 25–31.08 попадает в это множество (закроют её 3 сентября, но статус уже конечный),
    поэтому заработанное обнуляется как «уже в ведомости», а хвоста ведомости на 31 августа
    ещё нет — его дают только выплаты и начисления после финализации. Зарплата за целую
    неделю пропадала из среза насквозь.

    Проверяем обе даты и обе величины по отдельности: на 31.08 стоит ЗАРАБОТАННОЕ (28 000),
    на 03.09 — ХВОСТ ведомости (32 000). Суммы намеренно разные — совпади они, тест не
    отличил бы «взяли не ту величину» от «взяли правильную», а сумма 60 000 не появляется
    ни на одной дате: одни и те же деньги не считаются дважды.
    """
    async with async_session_factory() as session:
        cook = await _make_employee(session, name="Повар Августовский")
        period = await _make_week(
            session,
            start=date(2026, 8, 25),
            end=date(2026, 8, 31),
            payroll_date=date(2026, 9, 2),
            finalized_at=datetime(2026, 9, 3, 12, tzinfo=UTC),
        )
        run = await _make_run(session, period)
        await _make_line(session, run=run, employee=cook, total_payable="32000.00")
        session.add(_entry(cook.id, period.id, date(2026, 8, 26)))
        session.add(_entry(cook.id, period.id, date(2026, 8, 29)))
        await session.commit()

        async def fake_calc(_session, provisional, _run_id, entries, **_kwargs):
            """Формула заработка — чужой контур; здесь проверяется его ДАТИРОВКА."""
            assert provisional.end_date <= date(2026, 8, 31)
            line = PayrollLine(employee_id=cook.id, role="Повар", total_payable=Decimal("28000.00"))
            return PayrollCalculationResult(lines=[line], blocking_issues=[], summary={})

        monkeypatch.setattr(avail, "calculate_payroll_lines", fake_calc)

        august = await build_staff_balance_as_of(session, as_of=date(2026, 8, 31))
        august_row = _row(august, cook.id)
        assert august_row is not None, (
            "зарплата за неделю 25–31.08 исчезла из августа: период уже помечен finalized, "
            "но закрыт он 3 сентября — заработанное обнулили, а хвоста ещё нет"
        )
        assert august_row.salary == Decimal("28000.00"), (
            f"на 31.08 в долге {august_row.salary}: ожидалось заработанное 28000.00 — "
            "ни ноль (обнулили по статусу), ни хвост 32000.00 (ведомости в этот день ещё нет)"
        )

        september = await build_staff_balance_as_of(session, as_of=date(2026, 9, 3))
        september_row = _row(september, cook.id)
        assert september_row is not None, "хвост финализированной ведомости пропал из среза"
        assert september_row.salary == Decimal("32000.00"), (
            f"на 03.09 в долге {september_row.salary}: ожидался хвост ведомости 32000.00"
        )
        assert august_row.salary != september_row.salary, (
            "заработанное и хвост совпали — такой тест не отличил бы одну величину от другой"
        )
        for report in (august, september):
            assert report.salary_total < Decimal("60000.00"), (
                f"на {report.as_of} зарплата {report.salary_total} — заработанное сложено "
                "с хвостом той же недели, одни деньги посчитаны дважды"
            )


async def test_tail_stands_until_the_payment_transaction_date(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ведомость закрыта 28.07, выплачена 05.08: на 31.07 долг полный, на 05.08 — ноль.

    Дефект, который ловим, — гашение долга деньгами из будущего. Сегодня ведомость выплачена
    и в остатке её нет; на 31 июля она была живым обязательством на всю сумму начисления.
    Дату выплаты берём у проводки ДДС, а не у отметки: отметка живёт «сейчас».
    """
    async with async_session_factory() as session:
        cook = await _make_employee(session, name="Повар Июльский")
        wallet = await make_wallet(session, code="safe-staff-1", name="Сейф")
        period = await _make_week(
            session,
            start=date(2026, 7, 21),
            end=date(2026, 7, 27),
            payroll_date=date(2026, 7, 28),
            finalized_at=datetime(2026, 7, 28, 10, tzinfo=UTC),
        )
        run = await _make_run(session, period)
        await _make_line(session, run=run, employee=cook, total_payable="50000.00")
        await _make_booking(
            session,
            run=run,
            employee=cook,
            wallet_id=wallet.id,
            amount="50000.00",
            paid_on=date(2026, 8, 5),
        )
        await session.commit()

        july = await build_staff_balance_as_of(session, as_of=date(2026, 7, 31))
        july_row = _row(july, cook.id)
        assert july_row is not None, "августовская выплата закрыла июльский долг задним числом"
        assert july_row.salary == Decimal("50000.00")
        assert july.payable_total == Decimal("50000.00")

        august = await build_staff_balance_as_of(session, as_of=date(2026, 8, 5))
        assert _row(august, cook.id) is None, "выплата 05.08 не погасила долг в свою же дату"
        assert august.payable_total == ZERO


async def test_partial_payment_counts_only_the_tranche_paid_by_the_date(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Транш 40 000 от 20.08 и доплата 60 000 от 05.09: на 31.08 выплачено ровно 40 000.

    Два дефекта разом. Первый — дата последнего транша: по ней вся выплата уехала бы в
    сентябрь и на 31 августа долг стоял бы полный (100 000). Второй — бегущий итог строки
    ``payroll_payment``: её ``amount`` после доплаты равен 100 000, а ``paid_at`` показывает
    день первого транша, поэтому по ней на 31 августа выплаченным выглядит ВСЁ. Обе ловушки
    воспроизведены в данных: строка отметки заведена ровно в таком виде.
    """
    async with async_session_factory() as session:
        cook = await _make_employee(session, name="Повар Частичный")
        wallet = await make_wallet(session, code="safe-staff-2", name="Сейф")
        period = await _make_week(
            session,
            start=date(2026, 8, 11),
            end=date(2026, 8, 17),
            payroll_date=date(2026, 8, 18),
            finalized_at=datetime(2026, 8, 18, 10, tzinfo=UTC),
        )
        run = await _make_run(session, period)
        await _make_line(session, run=run, employee=cook, total_payable="100000.00")
        await _make_booking(
            session,
            run=run,
            employee=cook,
            wallet_id=wallet.id,
            amount="40000.00",
            paid_on=date(2026, 8, 20),
        )
        await _make_booking(
            session,
            run=run,
            employee=cook,
            wallet_id=wallet.id,
            amount="60000.00",
            paid_on=date(2026, 9, 5),
        )
        # Отметка выплаты в том виде, в каком её оставляет доплата: сумма — бегущий итог,
        # дата — день первого транша. Пара покрыта леджером, поэтому в расчёт она идти
        # не должна вовсе.
        session.add(
            PayrollPayment(
                id=uuid.uuid4(),
                run_id=run.id,
                employee_id=cook.id,
                amount=Decimal("100000.00"),
                status="paid",
                paid_at=date(2026, 8, 20),
            )
        )
        await session.commit()

        august = await build_staff_balance_as_of(session, as_of=date(2026, 8, 31))
        august_row = _row(august, cook.id)
        assert august_row is not None, (
            "на 31.08 долга нет вовсе — выплаченной посчитана вся сумма, включая "
            "сентябрьскую доплату"
        )
        assert august_row.salary == Decimal("60000.00"), (
            f"на 31.08 долг {august_row.salary}: выплаченным должен считаться ровно транш "
            "40000.00 — ни 0 (дата последнего транша), ни 100000.00 (бегущий итог отметки)"
        )
        assert august.approximate_payments == ZERO, (
            "выплаты взяты из отметки, хотя обе заведены в иммутабельный леджер"
        )

        september = await build_staff_balance_as_of(session, as_of=date(2026, 9, 5))
        assert _row(september, cook.id) is None, "доплата 05.09 не закрыла остаток"


async def test_unmarked_payment_is_visible_before_its_reversal_date(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Выплата 20.08, отметка снята 05.09: на 31.08 выплата ВИДНА, на 05.09 — уже нет.

    Решение владельца 08.08.2026: снятая отметка значит «деньги вернулись», и это событие
    СВОЕЙ даты. По строке ``payroll_payment`` этого не увидеть никогда — ``unmark_payment``
    удаляет её физически вместе со всей историей, и августовский срез показал бы долг,
    которого 31 августа не было. Строки отметки здесь поэтому нет вовсе: единственный след
    выплаты — booking и даты двух проводок.
    """
    async with async_session_factory() as session:
        cook = await _make_employee(session, name="Повар Отменённый")
        wallet = await make_wallet(session, code="safe-staff-3", name="Сейф")
        period = await _make_week(
            session,
            start=date(2026, 8, 11),
            end=date(2026, 8, 17),
            payroll_date=date(2026, 8, 18),
            finalized_at=datetime(2026, 8, 18, 10, tzinfo=UTC),
        )
        run = await _make_run(session, period)
        await _make_line(session, run=run, employee=cook, total_payable="30000.00")
        await _make_booking(
            session,
            run=run,
            employee=cook,
            wallet_id=wallet.id,
            amount="30000.00",
            paid_on=date(2026, 8, 20),
            reversed_on=date(2026, 9, 5),
        )
        await session.commit()

        august = await build_staff_balance_as_of(session, as_of=date(2026, 8, 31))
        assert _row(august, cook.id) is None, (
            "на 31.08 долг стоит: сентябрьское снятие отметки стёрло августовскую выплату"
        )
        assert august.payable_total == ZERO
        assert august.approximate_payments == ZERO, (
            "выплата взята из отметки — той самой строки, которую снятие удаляет"
        )

        september = await build_staff_balance_as_of(session, as_of=date(2026, 9, 5))
        september_row = _row(september, cook.id)
        assert september_row is not None, "деньги вернулись 05.09, а долг не восстановился"
        assert september_row.salary == Decimal("30000.00")


async def test_fund_of_dismissed_employee_lives_until_its_forfeit_date(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Фонд списан 10.09 при увольнении: на 31.08 он в кредиторке целиком, на 30.09 — ноль.

    Витрина теряет его на ОБЕ даты сразу: она держится за ``AccumulationFundAccount.status``
    и видимость сотрудника в ростере, а после увольнения счёт становится ``forfeited``, и
    статус этот действует задним числом на всю историю. 31 августа человек работал, и фонд
    был таким же обязательством, как зарплата; прибылью компании он стал 10 сентября — и
    ровно этой датой из среза уходит, потому что списание пришло датированной строкой
    леджера, а не сменой статуса.
    """
    async with async_session_factory() as session:
        cook = await _make_employee(
            session,
            name="Повар Уволенный",
            status="inactive",
            fire_date=date(2026, 9, 10),
        )
        account = AccumulationFundAccount(
            id=uuid.uuid4(),
            employee_id=cook.id,
            year=2026,
            accumulated_amount=Decimal("12000.00"),
            paid_out_amount=ZERO,
            forfeited_amount=Decimal("12000.00"),
            forfeited_at=datetime(2026, 9, 10, 12, tzinfo=UTC),
            status="forfeited",
        )
        session.add(account)
        await session.flush()
        session.add(
            AccumulationFundTransaction(
                id=uuid.uuid4(),
                account_id=account.id,
                employee_id=cook.id,
                year=2026,
                transaction_type="accrual",
                amount=Decimal("12000.00"),
                happened_on=date(2026, 7, 27),
                created_at=datetime(2026, 7, 28, 9, tzinfo=UTC),
            )
        )
        session.add(
            AccumulationFundTransaction(
                id=uuid.uuid4(),
                account_id=account.id,
                employee_id=cook.id,
                year=2026,
                transaction_type="forfeit",
                amount=Decimal("12000.00"),
                happened_on=date(2026, 9, 10),
                created_at=datetime(2026, 9, 10, 12, tzinfo=UTC),
            )
        )
        await session.commit()

        august = await build_staff_balance_as_of(session, as_of=date(2026, 8, 31))
        august_row = _row(august, cook.id)
        assert august_row is not None, (
            "фонд уволенного пропал из августа: статус счёта отработал задним числом"
        )
        assert august_row.fund == Decimal("12000.00")
        assert august_row.payable == Decimal("12000.00")

        september = await build_staff_balance_as_of(session, as_of=date(2026, 9, 30))
        assert _row(september, cook.id) is None, "списание 10.09 не убрало фонд из сентября"
        assert september.fund_total == ZERO


async def test_deposit_withholding_lands_in_the_month_it_was_earned(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Удержание депозита недели 25–31.08 стоит в августе, хотя ведомость закрыта 3 сентября.

    Решение владельца 08.08.2026: хозяйственная дата удержания — КОНЕЦ ПЕРИОДА РАБОТЫ
    (``happened_on``), а не день выплаты. По ``created_at`` эти деньги уехали бы в сентябрь:
    строка леджера рождается в момент финализации. Проверяем и вторую половину правила —
    что удержание встало РОВНО ОДИН РАЗ: депозит в срезе считается суммой леджера, и
    сложить её ещё и с полем счёта значило бы вернуть баг реконсиляции 09.07.2026.
    """
    async with async_session_factory() as session:
        cook = await _make_employee(session, name="Повар Депозитный")
        period = await _make_week(
            session,
            start=date(2026, 8, 25),
            end=date(2026, 8, 31),
            payroll_date=date(2026, 9, 2),
            finalized_at=datetime(2026, 9, 3, 12, tzinfo=UTC),
        )
        run = await _make_run(session, period)
        session.add(
            DepositTransaction(
                id=uuid.uuid4(),
                employee_id=cook.id,
                run_id=run.id,
                transaction_type="accrual",
                amount=Decimal("2000.00"),
                happened_on=date(2026, 8, 31),
                created_at=datetime(2026, 9, 3, 12, tzinfo=UTC),
            )
        )
        await session.commit()

        august = await build_staff_balance_as_of(session, as_of=date(2026, 8, 31))
        august_row = _row(august, cook.id)
        assert august_row is not None, (
            "удержание депозита за август не попало в августовский срез — датировано "
            "днём записи (3 сентября) вместо конца периода работы"
        )
        assert august_row.production_deposit == Decimal("2000.00"), (
            f"депозит {august_row.production_deposit} вместо 2000.00 — удержание посчитано "
            "не один раз"
        )
        assert august.production_deposit_total == Decimal("2000.00")

        before = await build_staff_balance_as_of(session, as_of=date(2026, 8, 30))
        assert _row(before, cook.id) is None, (
            "удержание видно 30.08 — раньше своей хозяйственной даты (31.08)"
        )


async def test_written_off_loan_stays_receivable_until_its_close_date(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Заём выдан 10.07, списан 12.09: на 31.08 дебиторка полная, на 30.09 — ноль.

    Витрина берёт только ``status='issued'``, поэтому списанный заём исчезает из ВСЕХ дат
    сразу — включая те, когда он был живым долгом сотрудника перед компанией. Срез смотрит
    на ``closed_on``: день, когда заём перестал быть долгом, а не текущее состояние строки.
    Списание позже 08.08.2026 ставит код, а не бэкфилл, — значит сумма точная и в
    ``approximate_write_offs`` попадать не должна.
    """
    async with async_session_factory() as session:
        cook = await _make_employee(session, name="Повар Заёмный")
        session.add(
            SalaryAdvance(
                id=uuid.uuid4(),
                employee_id=cook.id,
                role="Повар",
                kind="loan",
                amount=Decimal("20000.00"),
                per_installment_amount=Decimal("20000.00"),
                installments_count=1,
                recovered_amount=ZERO,
                status="written_off",
                issued_on=date(2026, 7, 10),
                closed_on=date(2026, 9, 12),
            )
        )
        await session.commit()

        august = await build_staff_balance_as_of(session, as_of=date(2026, 8, 31))
        august_row = _row(august, cook.id)
        assert august_row is not None, (
            "списанный в сентябре заём исчез из августа — срез смотрит на статус, а не на "
            "дату закрытия"
        )
        assert august_row.receivable == Decimal("20000.00")
        assert august.receivable_total == Decimal("20000.00")
        assert august.approximate_write_offs == ZERO, (
            "дата списания поставлена кодом (12.09), а сумма помечена приблизительной"
        )

        september = await build_staff_balance_as_of(session, as_of=date(2026, 9, 30))
        assert _row(september, cook.id) is None, "списание 12.09 не убрало дебиторку"
        assert september.receivable_total == ZERO


async def test_snapshot_refuses_dates_before_the_accounting_start(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """До 01.07.2026 срез отказывает, а не отдаёт ноль.

    Управленческий учёт начат этой датой, всё, что раньше, свёрнуто во входящие остатки.
    Ноль здесь был бы хуже ошибки: он выглядит как ответ «никому ничего не были должны» и
    молча уезжает в баланс. Та же граница и по той же причине стоит у признания расходов.
    """
    async with async_session_factory() as session:
        with pytest.raises(BeforeAccountingStart) as exc:
            await build_staff_balance_as_of(session, as_of=date(2026, 6, 30))
        assert "01.07.2026" in str(exc.value), (
            "в отказе нет даты начала учёта — по такому тексту непонятно, что вводить"
        )
        # Сама опорная дата уже внутри горизонта: отказ ровно на день раньше.
        report = await build_staff_balance_as_of(session, as_of=date(2026, 7, 1))
        assert report.as_of == date(2026, 7, 1)


async def test_route_rejects_early_date_with_422(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Роут отдаёт 422 на дату вне горизонта учёта и 200 внутри него.

    422, а не 409: открывать нечего — до 01.07.2026 движений в системе нет вовсе, и это
    негодный ВВОД, а не конфликт состояния. Заодно закреплено, что срез живёт отдельным
    адресом и витрину ``/staff-payable`` не трогает.
    """
    headers = await admin_headers(async_session_factory)
    early = client.get(
        "/api/v1/accounting/suppliers/staff-payable/as-of",
        params={"as_of": "2026-06-30"},
        headers=headers,
    )
    assert early.status_code == 422, early.text
    assert "01.07.2026" in early.text

    ok = client.get(
        "/api/v1/accounting/suppliers/staff-payable/as-of",
        params={"as_of": "2026-08-31"},
        headers=headers,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["as_of"] == "2026-08-31"


async def test_fund_of_a_run_still_in_flight_stays_out_of_the_snapshot(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ведомость «в полёте» не приносит ни депозита, ни фонда (решение владельца 08.08.2026).

    Асимметрия, из-за которой тест и написан: удержание депозита двигает счёт только при
    финализации, а фонд кредитуется ПРЯМО НА РАСЧЁТЕ — то есть строка фонда появляется у
    прогона в статусе ``completed``. Без фильтра авторитетности она попадала в обязательства,
    хотя ведомость ещё могла пересчитаться.
    """
    async with async_session_factory() as session:
        cook = await _make_employee(session, name="Повар Вполёте")
        period = await _make_week(
            session,
            start=date(2026, 8, 25),
            end=date(2026, 8, 31),
            payroll_date=date(2026, 9, 2),
        )
        run = await _make_run(session, period, status="completed")
        account = AccumulationFundAccount(
            id=uuid.uuid4(),
            employee_id=cook.id,
            year=2026,
            accumulated_amount=Decimal("5000.00"),
            paid_out_amount=ZERO,
            forfeited_amount=ZERO,
            status="active",
        )
        session.add(account)
        await session.flush()
        session.add(
            AccumulationFundTransaction(
                id=uuid.uuid4(),
                account_id=account.id,
                employee_id=cook.id,
                year=2026,
                run_id=run.id,
                transaction_type="accrual",
                amount=Decimal("5000.00"),
                happened_on=date(2026, 8, 31),
                created_at=datetime(2026, 8, 31, 20, tzinfo=UTC),
            )
        )
        session.add(
            DepositTransaction(
                id=uuid.uuid4(),
                employee_id=cook.id,
                run_id=run.id,
                transaction_type="accrual",
                amount=Decimal("2000.00"),
                happened_on=date(2026, 8, 31),
                created_at=datetime(2026, 8, 31, 20, tzinfo=UTC),
            )
        )
        await session.commit()

        snapshot = await build_staff_balance_as_of(session, as_of=date(2026, 8, 31))
        row = next((item for item in snapshot.rows if item.employee_id == cook.id), None)

        assert snapshot.fund_total == ZERO, "фонд незакрытого прогона не обязательство"
        assert snapshot.production_deposit_total == ZERO
        assert row is None or (row.fund == ZERO and row.production_deposit == ZERO)


async def test_payment_without_a_ledger_booking_falls_back_and_is_flagged(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Отметка выплаты без проводки в леджере — считаем, но помечаем приблизительной.

    Такие пары остались от старых ведомостей: ``PayrollPayment`` хранит бегущий итог, а не
    историю, и датировать его можно только последним касанием. Молча выдать эту цифру за факт
    значило бы соврать точностью; выкинуть — завысить долг на всю сумму.
    """
    async with async_session_factory() as session:
        cook = await _make_employee(session, name="Повар Безбукинга")
        period = await _make_week(
            session,
            start=date(2026, 7, 21),
            end=date(2026, 7, 27),
            payroll_date=date(2026, 7, 29),
            finalized_at=datetime(2026, 7, 28, 12, tzinfo=UTC),
        )
        run = await _make_run(session, period)
        await _make_line(session, run=run, employee=cook, total_payable="50000.00")
        session.add(
            PayrollPayment(
                id=uuid.uuid4(),
                run_id=run.id,
                employee_id=cook.id,
                amount=Decimal("30000.00"),
                paid_at=datetime(2026, 7, 29, 10, tzinfo=UTC),
            )
        )
        await session.commit()

        snapshot = await build_staff_balance_as_of(session, as_of=date(2026, 7, 31))
        row = next(item for item in snapshot.rows if item.employee_id == cook.id)

        # 50 000 начислено − 30 000 выплачено по отметке = 20 000 долга.
        assert row.salary == Decimal("20000.00")
        # И честно сказано, что 30 000 восстановлены не по леджеру.
        assert snapshot.approximate_payments == Decimal("30000.00")

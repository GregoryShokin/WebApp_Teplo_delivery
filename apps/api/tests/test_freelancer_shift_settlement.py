"""Посменная выдача внештатника из кассы (вариант Б): список по людям + выдача по сменам,
сверка с ведомостью (без двойной выплаты).

Источник данных — явки ``attendance_entry`` открытого недельного периода (тот же
пайплайн, что кормит авансы производственников), НЕ отдельный iiko-синк.

- список: одна строка на внештатника (Σ неоплаченных смен открытого периода);
- детализация: смены из attendance_entry, суммы = ставка/12 × часы, оплаченные помечены;
- «Выплатить»: out-проводка из ТК Черникова по статье производственной ЗП движком,
  мультивыбор по attendance_entry_id, paid_cash-записи; повтор смены — 409; сумма>остатка не блок;
- ведомость исключает paid_cash из «к выплате» (в ФОТ остаётся); staleness-гейт;
- «Синхронизировать» дёргает refresh_current_week_advance_window (мок iiko).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta, tzinfo
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    AttendanceEntry,
    CashflowTransaction,
    DdsArticle,
    Employee,
    FreelancerShiftSettlement,
    PayrollLine,
    PayrollPeriod,
    PayrollRun,
    Wallet,
)
from app.services.freelancer import shift_settlement
from app.services.freelancer.shift_settlement import (
    FREELANCER_SHIFT_ARTICLE_CODE,
    FREELANCER_SHIFT_PAYOUT_SOURCE_KIND,
    FreelancerShiftSettlementError,
    freelancer_shift_details,
    list_unpaid_freelancers,
    pay_freelancer_shifts,
    sync_freelancer_shifts,
)
from app.services.kassa.payouts import KASSA_WALLET_CODE, kassa_pending_payload
from app.services.payroll_calculator import base_shift_pay
from app.services.payroll_freelancer_settlement import (
    apply_freelancer_cash_settlements,
    run_has_stale_freelancer_settlements,
)

# Открытая ЗП-неделя вт(07.07)→пн(13.07); as_of внутри неё.
PERIOD_START = date(2026, 7, 7)
PERIOD_END = date(2026, 7, 13)
PAYROLL_DATE = date(2026, 7, 14)
AS_OF = date(2026, 7, 8)


def _freeze_settlement_clock(monkeypatch: pytest.MonkeyPatch, *, today: date = AS_OF) -> None:
    """Заморозить «сегодня» внутри shift_settlement на дату открытого периода.

    Часть API (``kassa_pending_payload``) не пробрасывает ``as_of`` — модуль берёт
    ``datetime.now(MOSCOW_TZ).date()``. Без заморозки такой тест зелёный только пока
    реальная дата попадает в зашитый период (или в фолбэк «ближайший будущий»), а потом
    молча краснеет. Подменяем импортированный в модуль ``datetime``.
    """

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:  # type: ignore[override]
            return datetime(today.year, today.month, today.day, 12, 0, tzinfo=tz)

    monkeypatch.setattr(shift_settlement, "datetime", _FrozenDatetime)


# --------------------------------------------------------------------------- #
# Фабрики                                                                      #
# --------------------------------------------------------------------------- #
async def _freelancer(
    session: AsyncSession, *, name: str = "Иван Внештат", rate: Decimal | None = Decimal("3600")
) -> Employee:
    employee = Employee(
        id=uuid.uuid4(),
        full_name=name,
        iiko_id=f"freelancer:{uuid.uuid4().hex}",
        category="freelancer",
        status="active",
        is_freelancer_temp=True,
        freelancer_shift_rate=rate,
        pin_hash="hashed-pin",
        pin_set_at=datetime(2026, 1, 1, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add(employee)
    await session.flush()
    return employee


async def _period(
    session: AsyncSession,
    *,
    start: date = PERIOD_START,
    end: date = PERIOD_END,
    status: str = "open",
) -> PayrollPeriod:
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="week",
        start_date=start,
        end_date=end,
        payroll_date=PAYROLL_DATE,
        status=status,
    )
    session.add(period)
    await session.flush()
    return period


async def _attendance(
    session: AsyncSession,
    employee_id: uuid.UUID,
    period_id: uuid.UUID,
    *,
    work_date: date = PERIOD_START,
    minutes: int | None = 720,
    started_hour: int = 10,
) -> AttendanceEntry:
    started_at = datetime(work_date.year, work_date.month, work_date.day, started_hour, tzinfo=UTC)
    ended_at = started_at + timedelta(minutes=minutes) if minutes is not None else None
    entry = AttendanceEntry(
        id=uuid.uuid4(),
        employee_id=employee_id,
        period_id=period_id,
        work_date=work_date,
        started_at=started_at,
        ended_at=ended_at,
        minutes_worked=minutes or 0,
        source="iiko",
    )
    session.add(entry)
    await session.flush()
    return entry


async def _tk_wallet(session: AsyncSession) -> Wallet:
    wallet = await session.scalar(select(Wallet).where(Wallet.code == KASSA_WALLET_CODE))
    assert wallet is not None, "кошелёк ТК Черникова должен быть в сидах"
    return wallet


async def _payroll_article(session: AsyncSession) -> DdsArticle:
    article = await session.scalar(
        select(DdsArticle).where(DdsArticle.code == FREELANCER_SHIFT_ARTICLE_CODE)
    )
    assert article is not None, "статья производственной ЗП должна быть в сидах"
    return article


def _line(
    employee_id: uuid.UUID,
    *,
    base_pay: Decimal,
    total_payable: Decimal,
    role: str = "sushi",
) -> PayrollLine:
    return PayrollLine(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        employee_id=employee_id,
        role=role,
        base_pay=base_pay,
        total_payable=total_payable,
        components={},
    )


async def _run(session: AsyncSession, period: PayrollPeriod) -> PayrollRun:
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=period.id,
        started_at=datetime(2026, 7, 14, tzinfo=UTC),
        status="running",
        blocking_issues=[],
        summary={},
    )
    session.add(run)
    await session.flush()
    return run


# --------------------------------------------------------------------------- #
# 1. Список по людям                                                           #
# --------------------------------------------------------------------------- #
async def test_list_one_row_per_freelancer_sum_of_unpaid(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _freelancer(session, rate=Decimal("3600"))
        period = await _period(session)
        await _attendance(session, employee.id, period.id, work_date=date(2026, 7, 7), minutes=720)
        await _attendance(session, employee.id, period.id, work_date=date(2026, 7, 8), minutes=720)
        await _attendance(session, employee.id, period.id, work_date=date(2026, 7, 9), minutes=360)
        await session.commit()

        rows = await list_unpaid_freelancers(session, as_of=AS_OF)
        assert len(rows) == 1  # ОДНА строка на внештатника
        row = rows[0]
        assert row["employee_id"] == employee.id
        assert row["shift_count"] == 3
        # 3600 + 3600 + 1800 = 9000
        assert row["unpaid_total"] == 9000.0


async def test_list_shows_remainder_after_paying_one_shift(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _tk_wallet(session)
        await _payroll_article(session)
        employee = await _freelancer(session, rate=Decimal("3600"))
        period = await _period(session)
        e1 = await _attendance(session, employee.id, period.id, work_date=date(2026, 7, 7))
        await _attendance(session, employee.id, period.id, work_date=date(2026, 7, 8))
        await _attendance(session, employee.id, period.id, work_date=date(2026, 7, 9))
        await session.commit()

        # Оплатили одну смену → в строке остаток Σ по двум.
        await pay_freelancer_shifts(
            session, attendance_entry_ids=[e1.id], actor_user_id=None, today=AS_OF
        )
        rows = await list_unpaid_freelancers(session, as_of=AS_OF)
        assert len(rows) == 1
        assert rows[0]["shift_count"] == 2
        assert rows[0]["unpaid_total"] == 7200.0


async def test_list_empty_without_open_period(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        # Явок/периода нет вовсе — не падаем, пусто.
        assert await list_unpaid_freelancers(session, as_of=AS_OF) == []


async def test_list_ignores_zero_rate_and_open_shifts(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        period = await _period(session)
        no_rate = await _freelancer(session, name="Без ставки", rate=None)
        await _attendance(session, no_rate.id, period.id)
        open_shift_emp = await _freelancer(session, name="Открытая смена", rate=Decimal("3600"))
        await _attendance(session, open_shift_emp.id, period.id, minutes=None)  # нет ended_at
        await session.commit()

        # Ставка 0 и смена без ended_at не дают ни строк, ни сумм.
        assert await list_unpaid_freelancers(session, as_of=AS_OF) == []


async def test_list_ignores_non_freelancers(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        period = await _period(session)
        staff = Employee(
            id=uuid.uuid4(),
            full_name="Штатный Повар",
            iiko_id=f"staff:{uuid.uuid4().hex}",
            category="category_2",
            status="active",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        session.add(staff)
        await session.flush()
        await _attendance(session, staff.id, period.id)
        await session.commit()

        assert await list_unpaid_freelancers(session, as_of=AS_OF) == []


# --------------------------------------------------------------------------- #
# 2. Детализация (модалка)                                                     #
# --------------------------------------------------------------------------- #
async def test_details_amounts_match_formula_and_mark_paid(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _tk_wallet(session)
        await _payroll_article(session)
        employee = await _freelancer(session, rate=Decimal("3600"))
        period = await _period(session)
        full = await _attendance(session, employee.id, period.id, work_date=date(2026, 7, 7), minutes=720)
        partial = await _attendance(
            session, employee.id, period.id, work_date=date(2026, 7, 8), minutes=660
        )
        await session.commit()

        details = await freelancer_shift_details(session, employee.id, as_of=AS_OF)
        by_entry = {row["attendance_entry_id"]: row for row in details}
        assert len(details) == 2
        # Суммы = ставка/12 × часы (та же формула, что в калькуляторе ЗП).
        assert by_entry[full.id]["amount"] == float(
            base_shift_pay({}, "sushi", "freelancer", employee, 720).quantize(Decimal("0.01"))
        )
        assert by_entry[full.id]["amount"] == 3600.0
        assert by_entry[full.id]["hours"] == 12.0
        assert by_entry[partial.id]["amount"] == 3300.0  # 3600 × 660/720
        assert all(row["paid"] is False for row in details)

        # После оплаты одной смены — она помечена paid, вторая нет.
        await pay_freelancer_shifts(
            session, attendance_entry_ids=[full.id], actor_user_id=None, today=AS_OF
        )
        details = await freelancer_shift_details(session, employee.id, as_of=AS_OF)
        by_entry = {row["attendance_entry_id"]: row for row in details}
        assert by_entry[full.id]["paid"] is True
        assert by_entry[partial.id]["paid"] is False


# --------------------------------------------------------------------------- #
# 3. Выдача из кассы                                                           #
# --------------------------------------------------------------------------- #
async def test_payout_books_cashflow_from_tk_by_payroll_article(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        wallet = await _tk_wallet(session)
        article = await _payroll_article(session)
        employee = await _freelancer(session, rate=Decimal("3600"))
        period = await _period(session)
        e1 = await _attendance(session, employee.id, period.id, work_date=date(2026, 7, 7))
        e2 = await _attendance(session, employee.id, period.id, work_date=date(2026, 7, 8))
        await session.commit()

        # Мультивыбор: две смены → две проводки + две paid_cash-записи.
        txn_ids = await pay_freelancer_shifts(
            session, attendance_entry_ids=[e1.id, e2.id], actor_user_id=None, today=AS_OF
        )
        assert len(txn_ids) == 2

        for entry_id in (e1.id, e2.id):
            settlement = await session.scalar(
                select(FreelancerShiftSettlement).where(
                    FreelancerShiftSettlement.attendance_entry_id == entry_id
                )
            )
            assert settlement is not None
            assert settlement.status == "paid_cash"
            assert settlement.amount == Decimal("3600.00")
            assert settlement.period_id == period.id
            assert settlement.cashflow_transaction_id is not None
            txn = await session.get(CashflowTransaction, settlement.cashflow_transaction_id)
            assert txn.wallet_id == wallet.id
            assert txn.direction == "out"
            assert txn.amount == Decimal("3600.00")
            assert txn.article_id == article.id
            assert txn.source_kind == FREELANCER_SHIFT_PAYOUT_SOURCE_KIND
            assert txn.source_id == entry_id
            assert txn.quality_status == "final"


async def test_payout_already_paid_conflicts(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _tk_wallet(session)
        await _payroll_article(session)
        employee = await _freelancer(session)
        period = await _period(session)
        entry = await _attendance(session, employee.id, period.id)
        await session.commit()

        await pay_freelancer_shifts(
            session, attendance_entry_ids=[entry.id], actor_user_id=None, today=AS_OF
        )
        # Повторная выдача той же смены — 409.
        with pytest.raises(FreelancerShiftSettlementError):
            await pay_freelancer_shifts(
                session, attendance_entry_ids=[entry.id], actor_user_id=None, today=AS_OF
            )


async def test_payout_over_balance_not_blocked(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _tk_wallet(session)  # пустой остаток
        await _payroll_article(session)
        employee = await _freelancer(session)
        period = await _period(session)
        entry = await _attendance(session, employee.id, period.id)
        await session.commit()

        # Сумма больше остатка кассы не блокируется (предупреждение — на фронте).
        txn_ids = await pay_freelancer_shifts(
            session, attendance_entry_ids=[entry.id], actor_user_id=None, today=AS_OF
        )
        assert len(txn_ids) == 1


async def test_payout_unknown_entry_conflicts(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _tk_wallet(session)
        await _payroll_article(session)
        await _period(session)
        await session.commit()

        with pytest.raises(FreelancerShiftSettlementError):
            await pay_freelancer_shifts(
                session, attendance_entry_ids=[uuid.uuid4()], actor_user_id=None, today=AS_OF
            )


# --------------------------------------------------------------------------- #
# 4. Стык с ведомостью (без двойной выплаты)                                   #
# --------------------------------------------------------------------------- #
async def test_paid_cash_excluded_from_payable_but_kept_in_fot(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _tk_wallet(session)
        await _payroll_article(session)
        employee = await _freelancer(session, rate=Decimal("3600"))
        period = await _period(session)
        entry = await _attendance(session, employee.id, period.id, work_date=date(2026, 7, 7))
        await session.commit()
        await pay_freelancer_shifts(
            session, attendance_entry_ids=[entry.id], actor_user_id=None, today=AS_OF
        )

        run = await _run(session, period)
        line = _line(employee.id, base_pay=Decimal("3600.00"), total_payable=Decimal("3600.00"))
        summary = await apply_freelancer_cash_settlements(session, period, run, [line])

        # «К выплате» обнулилось, но gross/ФОТ остался.
        assert line.total_payable == Decimal("0.00")
        assert line.base_pay == Decimal("3600.00")
        assert line.components["freelancer_cash_settled"] == 3600.0
        assert summary["freelancer_cash_settled_count"] == 1
        assert summary["freelancer_paid_cash_total"] == 3600.0
        assert summary["freelancer_paid_cash_count"] == 1


async def test_paid_cash_clamped_to_net(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _tk_wallet(session)
        await _payroll_article(session)
        employee = await _freelancer(session, rate=Decimal("3600"))
        period = await _period(session)
        entry = await _attendance(session, employee.id, period.id, work_date=date(2026, 7, 7))
        await session.commit()
        await pay_freelancer_shifts(
            session, attendance_entry_ids=[entry.id], actor_user_id=None, today=AS_OF
        )

        run = await _run(session, period)
        # net строки меньше выданного налом (напр. после удержаний) → вычет клэмпится.
        line = _line(employee.id, base_pay=Decimal("3600.00"), total_payable=Decimal("1000.00"))
        summary = await apply_freelancer_cash_settlements(session, period, run, [line])
        assert line.total_payable == Decimal("0.00")
        assert line.components["freelancer_cash_settled"] == 1000.0
        # Подпись хранит ПОЛНЫЙ итог cash-смен периода (для staleness), не клэмп.
        assert summary["freelancer_paid_cash_total"] == 3600.0


async def test_paid_cash_of_other_period_not_excluded(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _tk_wallet(session)
        await _payroll_article(session)
        employee = await _freelancer(session, rate=Decimal("3600"))
        other = await _period(
            session, start=date(2026, 6, 30), end=date(2026, 7, 6), status="open"
        )
        entry = await _attendance(session, employee.id, other.id, work_date=date(2026, 6, 30))
        await session.commit()
        # Оплачена налом смена ДРУГОГО периода.
        await pay_freelancer_shifts(
            session, attendance_entry_ids=[entry.id], actor_user_id=None, today=date(2026, 7, 1)
        )

        target = await _period(session)  # текущая неделя
        run = await _run(session, target)
        line = _line(employee.id, base_pay=Decimal("3600.00"), total_payable=Decimal("3600.00"))
        summary = await apply_freelancer_cash_settlements(session, target, run, [line])
        # Сверка ведёт по period_id — чужой период не касается.
        assert line.total_payable == Decimal("3600.00")
        assert summary["freelancer_cash_settled_count"] == 0
        assert summary["freelancer_paid_cash_count"] == 0


async def test_stale_guard_detects_cash_paid_after_calc(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _tk_wallet(session)
        await _payroll_article(session)
        employee = await _freelancer(session, rate=Decimal("3600"))
        period = await _period(session)
        entry = await _attendance(session, employee.id, period.id, work_date=date(2026, 7, 7))
        await session.commit()

        # Расчёт ведомости: cash-смен пока нет → подпись зафиксирована.
        run = await _run(session, period)
        line = _line(employee.id, base_pay=Decimal("3600.00"), total_payable=Decimal("3600.00"))
        run.summary = await apply_freelancer_cash_settlements(session, period, run, [line])
        await session.commit()
        assert await run_has_stale_freelancer_settlements(session, run, period) is False

        # После расчёта смену выдали наличными → ведомость устарела.
        await pay_freelancer_shifts(
            session, attendance_entry_ids=[entry.id], actor_user_id=None, today=AS_OF
        )
        assert await run_has_stale_freelancer_settlements(session, run, period) is True


async def test_finalized_period_drops_from_kassa_paid_cash_stays_excluded(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await _tk_wallet(session)
        await _payroll_article(session)
        employee = await _freelancer(session, rate=Decimal("3600"))
        period = await _period(session)
        paid = await _attendance(session, employee.id, period.id, work_date=date(2026, 7, 7))
        await _attendance(session, employee.id, period.id, work_date=date(2026, 7, 8))
        await session.commit()
        await pay_freelancer_shifts(
            session, attendance_entry_ids=[paid.id], actor_user_id=None, today=AS_OF
        )

        # До финализации: одна неоплаченная смена видна в кассе.
        rows = await list_unpaid_freelancers(session, as_of=AS_OF)
        assert rows and rows[0]["shift_count"] == 1

        # Финализация периода → он уходит из «открытых».
        period.status = "finalized"
        await session.commit()

        # Смены периода исчезают из кассы (неоплаченные платит ведомость, в gross).
        assert await list_unpaid_freelancers(session, as_of=AS_OF) == []
        # Но paid_cash периода по-прежнему вычитается из net ведомости.
        run = await _run(session, period)
        line = _line(employee.id, base_pay=Decimal("7200.00"), total_payable=Decimal("7200.00"))
        summary = await apply_freelancer_cash_settlements(session, period, run, [line])
        assert line.total_payable == Decimal("3600.00")  # вычтена оплаченная налом
        assert summary["freelancer_paid_cash_count"] == 1


# --------------------------------------------------------------------------- #
# 5. «К выдаче» и синк                                                          #
# --------------------------------------------------------------------------- #
async def test_kassa_pending_includes_freelancers_per_person(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # kassa_pending_payload не принимает as_of — «сегодня» берётся из часов модуля.
    _freeze_settlement_clock(monkeypatch)
    async with async_session_factory() as session:
        await _tk_wallet(session)
        employee = await _freelancer(session, name="Пётр Внештат", rate=Decimal("3600"))
        period = await _period(session)
        await _attendance(session, employee.id, period.id, work_date=date(2026, 7, 7))
        await _attendance(session, employee.id, period.id, work_date=date(2026, 7, 8))
        await session.commit()

        payload = await kassa_pending_payload(session)
        assert len(payload["freelancers"]) == 1  # одна строка на человека
        row = payload["freelancers"][0]
        assert row["name"] == "Пётр Внештат"
        assert row["unpaid_total"] == 7200.0
        assert row["shift_count"] == 2
        assert payload["pending_count"] == 1


async def test_sync_calls_refresh_window_and_returns_list(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with async_session_factory() as session:
        employee = await _freelancer(session, rate=Decimal("3600"))
        period = await _period(session)
        # Явка уже в attendance_entry (как будто ночное окно её принесло) — мок «дёргает» iiko.
        await _attendance(session, employee.id, period.id, work_date=date(2026, 7, 7))
        await session.commit()

        called: dict[str, object] = {}

        async def _fake_refresh(sess, *, today=None):  # noqa: ANN001
            called["today"] = today
            return period

        monkeypatch.setattr(
            "app.services.payroll_runner.refresh_current_week_advance_window", _fake_refresh
        )

        report = await sync_freelancer_shifts(session, today=AS_OF)
        assert "today" in called  # окно авансов дёрнуто (iiko-сейм внутри)
        assert called["today"] == AS_OF
        assert len(report["freelancers"]) == 1
        assert report["freelancers"][0]["unpaid_total"] == 3600.0

"""Фаза 10: недостача смены > порога % выручки → авто-штраф кассирам.

Проверяет порог (1 % выручки из настройки), делёж недостачи РОВНО поровну между
кассирами смены (явка роли «administrator» в «Учёте смен» на дату открытия),
идемпотентность повторного синка, отмену штрафа (удаляет PayrollAdjustment из расчёта)
и ветку «нет кассиров → ручной разбор». iiko-фетчи замоканы. Прогон на ``teplo_test``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.kassa.iiko_cashshift_sync as sync_mod
from app.models import Employee, KassaShiftPenalty, PayrollAdjustment, ShiftLedgerEntry, User
from app.services.kassa.iiko_cashshift_sync import (
    CASHIER_POSITION,
    PENALTY_STATUS_APPLIED,
    PENALTY_STATUS_MANUAL_REVIEW,
    PENALTY_STATUS_NONE,
    PENALTY_STATUS_WAIVED,
    sync_iiko_cashshifts,
    waive_shift_penalty,
)
from app.services.payroll_adjustment_service import load_adjustments_for_period

DF = date(2026, 6, 15)
DT = date(2026, 6, 18)
WORK_DATE = date(2026, 6, 17)


def _shift(
    session_id: str = "sess-pen",
    *,
    cash_sales: float = 50000,
    pay_out: float = 49000,
    start_cash: float = 5000,
    cash_remain: float = 5000,
    pay_in: float = 0,
) -> dict:
    """Смена с управляемым расхождением: diff = cash_sales+pay_in-pay_out-(remain-start)."""
    return {
        "id": session_id,
        "sessionNumber": 1200,
        "pointOfSaleId": "pos-1",
        "managerId": "mgr-1",
        "openDate": f"{WORK_DATE.isoformat()}T08:30:00",
        "closeDate": f"{WORK_DATE.isoformat()}T23:00:00",
        "sessionStatus": "UNACCEPTED",
        "sessionStartCash": start_cash,
        "salesCash": cash_sales,
        "salesCard": 0,
        "payIn": pay_in,
        "payOut": pay_out,
        "cashRemain": cash_remain,
        "cashDiff": 0,
    }


def _patch(
    monkeypatch,
    shifts: list[dict],
    *,
    cash: Decimal,
    total: Decimal,
) -> None:
    cash_map = {WORK_DATE: cash}
    total_map = {WORK_DATE: total}
    monkeypatch.setattr(sync_mod, "_auth_token", lambda: "tok")
    monkeypatch.setattr(sync_mod, "_fetch_cashshifts_list", lambda token, df, dt: shifts)
    monkeypatch.setattr(sync_mod, "_fetch_shift_payments", lambda token, sid: {})
    monkeypatch.setattr(sync_mod, "_fetch_accounts_map", lambda token: {})
    monkeypatch.setattr(
        sync_mod, "_fetch_cash_sales_by_day", lambda token, df, dt: (cash_map, total_map)
    )


async def _make_cashiers(session: AsyncSession, count: int) -> list[uuid.UUID]:
    """count кассиров с явкой роли administrator на WORK_DATE («Учёт смен»)."""
    ids: list[uuid.UUID] = []
    for index in range(count):
        employee = Employee(
            full_name=f"Кассир {index + 1}",
            iiko_id=f"iiko-cashier-{uuid.uuid4().hex[:8]}",
        )
        session.add(employee)
        await session.flush()
        session.add(
            ShiftLedgerEntry(
                work_date=WORK_DATE,
                employee_id=employee.id,
                payroll_role="administrator",
                opened_at=datetime(2026, 6, 17, 10 + index, 0, tzinfo=UTC),
                source="schedule",
            )
        )
        ids.append(employee.id)
    await session.flush()
    return ids


async def _penalties(session: AsyncSession, shift_id) -> list[KassaShiftPenalty]:
    return list(
        (
            await session.scalars(
                select(KassaShiftPenalty).where(KassaShiftPenalty.shift_id == shift_id)
            )
        ).all()
    )


async def test_below_threshold_no_penalty(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # Недостача 100 ₽ при выручке 50000 (порог 1% = 500) → штрафа нет.
    _patch(monkeypatch, [_shift(pay_out=49900)], cash=Decimal("50000"), total=Decimal("50000"))
    async with async_session_factory() as session:
        await _make_cashiers(session, 2)
        report = await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()

        assert report.penalized == 0
        shift = await session.scalar(select(sync_mod.IikoCashShift))
        assert shift.penalty_status == PENALTY_STATUS_NONE
        assert await _penalties(session, shift.id) == []
        adj_count = await session.scalar(select(func.count()).select_from(PayrollAdjustment))
        assert adj_count == 0


async def test_above_threshold_splits_evenly(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # Недостача 1000 ₽ (выручка 50000, порог 500) → штраф 1000 на 2 кассиров = 500/500.
    _patch(monkeypatch, [_shift(pay_out=49000)], cash=Decimal("50000"), total=Decimal("50000"))
    async with async_session_factory() as session:
        cashier_ids = await _make_cashiers(session, 2)
        report = await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()

        assert report.penalized == 1
        shift = await session.scalar(select(sync_mod.IikoCashShift))
        assert shift.penalty_status == PENALTY_STATUS_APPLIED

        penalties = await _penalties(session, shift.id)
        assert len(penalties) == 2
        assert {p.employee_id for p in penalties} == set(cashier_ids)
        assert sum(p.amount for p in penalties) == Decimal("1000.00")
        assert all(p.amount == Decimal("500.00") for p in penalties)
        assert all(p.status == "active" for p in penalties)

        # Каждому кассиру создан PayrollAdjustment (penalty, роль «Кассир») в дату смены.
        adjustments = list((await session.scalars(select(PayrollAdjustment))).all())
        assert len(adjustments) == 2
        assert all(a.type == "penalty" for a in adjustments)
        assert all(a.role == CASHIER_POSITION for a in adjustments)
        assert all(a.work_date == WORK_DATE for a in adjustments)
        assert {a.id for a in adjustments} == {p.payroll_adjustment_id for p in penalties}


async def test_split_distributes_remainder(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # Недостача 1000.01 на 2 → доли 500.01 и 500.00 (остаток копейки одной доле).
    _patch(
        monkeypatch,
        [_shift(cash_sales=50000.01, pay_out=49000)],
        cash=Decimal("50000.01"),
        total=Decimal("50000.01"),
    )
    async with async_session_factory() as session:
        await _make_cashiers(session, 2)
        await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()

        shift = await session.scalar(select(sync_mod.IikoCashShift))
        penalties = await _penalties(session, shift.id)
        amounts = sorted(p.amount for p in penalties)
        assert amounts == [Decimal("500.00"), Decimal("500.01")]
        assert sum(amounts) == Decimal("1000.01")


async def test_idempotent_resync_no_duplicate(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _patch(monkeypatch, [_shift(pay_out=49000)], cash=Decimal("50000"), total=Decimal("50000"))
    async with async_session_factory() as session:
        await _make_cashiers(session, 2)
        await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()
        report2 = await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()

        assert report2.penalized == 0  # повторный синк не штрафует заново
        shift = await session.scalar(select(sync_mod.IikoCashShift))
        assert len(await _penalties(session, shift.id)) == 2
        adj_count = await session.scalar(select(func.count()).select_from(PayrollAdjustment))
        assert adj_count == 2


async def test_waive_removes_from_payroll(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _patch(monkeypatch, [_shift(pay_out=49000)], cash=Decimal("50000"), total=Decimal("50000"))
    async with async_session_factory() as session:
        cashier_ids = await _make_cashiers(session, 2)
        await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()

        shift = await session.scalar(select(sync_mod.IikoCashShift))
        actor = User(
            email=f"waiver-{uuid.uuid4().hex[:8]}@test.local",
            hashed_password="hashed",
            full_name="Владелец",
            is_active=True,
        )
        session.add(actor)
        await session.flush()
        actor_id = actor.id
        waived = await waive_shift_penalty(session, shift, actor_user_id=actor_id)
        await session.commit()
        assert waived == 2

        # PayrollAdjustment удалены → недостача ушла из расчёта зарплаты.
        adj_count = await session.scalar(select(func.count()).select_from(PayrollAdjustment))
        assert adj_count == 0
        loaded = await load_adjustments_for_period(
            session,
            employee_ids=cashier_ids,
            period_start=WORK_DATE,
            period_end=WORK_DATE,
        )
        assert loaded == {}

        penalties = await _penalties(session, shift.id)
        assert all(p.status == "waived" for p in penalties)
        assert all(p.payroll_adjustment_id is None for p in penalties)
        assert all(p.waived_by_user_id == actor_id for p in penalties)
        assert shift.penalty_status == PENALTY_STATUS_WAIVED

        # Повторный синк не воскрешает отменённый штраф.
        report3 = await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()
        assert report3.penalized == 0
        assert await session.scalar(select(func.count()).select_from(PayrollAdjustment)) == 0
        shift = await session.scalar(select(sync_mod.IikoCashShift))
        assert shift.penalty_status == PENALTY_STATUS_WAIVED


async def test_no_cashiers_marks_manual_review(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # Недостача > порога, но кассиров на дату нет → ручной разбор, штраф не создаётся.
    _patch(monkeypatch, [_shift(pay_out=49000)], cash=Decimal("50000"), total=Decimal("50000"))
    async with async_session_factory() as session:
        report = await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()

        assert report.penalized == 0
        shift = await session.scalar(select(sync_mod.IikoCashShift))
        assert shift.penalty_status == PENALTY_STATUS_MANUAL_REVIEW
        assert shift.penalty_review_reason is not None
        assert await _penalties(session, shift.id) == []
        adj_count = await session.scalar(select(func.count()).select_from(PayrollAdjustment))
        assert adj_count == 0
